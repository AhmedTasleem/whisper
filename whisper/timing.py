import subprocess
import warnings
from dataclasses import dataclass
from typing import List, TYPE_CHECKING

import numba
import numpy as np
import torch
import torch.nn.functional as F

from .audio import HOP_LENGTH, SAMPLE_RATE, TOKENS_PER_SECOND
from .tokenizer import Tokenizer

if TYPE_CHECKING:
    from .model import Whisper


def median_filter(x: torch.Tensor, filter_width: int):
    """Apply a median filter of width `filter_width` along the last dimension of `x`"""
    pad_width = filter_width // 2
    if x.shape[-1] <= pad_width:
        # F.pad requires the padding width to be smaller than the input dimension
        return x

    if (ndim := x.ndim) <= 2:
        # `F.pad` does not support 1D or 2D inputs for reflect padding but supports 3D and 4D
        x = x[None, None, :]

    assert filter_width > 0 and filter_width % 2 == 1, "`filter_width` should be an odd number"

    result = None
    x = F.pad(x, (filter_width // 2, filter_width // 2, 0, 0), mode="reflect")
    if x.is_cuda:
        try:
            from .triton_ops import median_filter_cuda
            result = median_filter_cuda(x, filter_width)
        except (RuntimeError, subprocess.CalledProcessError):
            warnings.warn(
                "Failed to launch Triton kernels, likely due to missing CUDA toolkit; "
                "falling back to a slower median kernel implementation..."
            )

    if result is None:
        # sort() is faster than torch.median (https://github.com/pytorch/pytorch/issues/51450)
        result = x.unfold(-1, filter_width, 1).sort()[0][..., filter_width // 2]

    if ndim <= 2:
        result = result[0, 0]

    return result

@numba.jit
def backtrace(trace: np.ndarray):
    i = trace.shape[0] - 1
    j = trace.shape[1] - 1
    trace[0, :] = 2
    trace[:, 0] = 1

    result = []
    while i > 0 or j > 0:
        result.append((i - 1, j - 1))

        if trace[i, j] == 0:
            i -= 1
            j -= 1
        elif trace[i, j] == 1:
            i -= 1
        elif trace[i, j] == 2:
            j -= 1
        else:
            raise ValueError("Unexpected trace[i, j]")

    result = np.array(result)
    return result[::-1, :].T


@numba.jit(nopython=True, parallel=True)
def dtw_cpu(x: np.ndarray):
    N, M = x.shape
    cost = np.ones((N + 1, M + 1), dtype=np.float32) * np.inf
    trace = -np.ones((N + 1, M + 1), dtype=np.float32)

    cost[0, 0] = 0
    for j in range(1, M + 1):
        for i in range(1, N + 1):
            c0 = cost[i - 1, j - 1]
            c1 = cost[i - 1, j]
            c2 = cost[i, j - 1]

            if c0 < c1 and c0 < c2:
                c, t = c0, 0
            elif c1 < c0 and c1 < c2:
                c, t = c1, 1
            else:
                c, t = c2, 2

            cost[i, j] = x[i - 1, j - 1] + c
            trace[i, j] = t

    return backtrace(trace)


def dtw_cuda(x, BLOCK_SIZE=1024):
    from .triton_ops import dtw_kernel

    M, N = x.shape
    assert M < BLOCK_SIZE, f"M should be smaller than {BLOCK_SIZE=}"

    x_skew = F.pad(x, (0, M + 1), value=np.inf).flatten()[: M * (N + M)].reshape(M, N + M)
    x_skew = x_skew.T.contiguous()
    cost = torch.ones(N + M + 2, M + 2) * np.inf
    cost[0, 0] = 0
    cost = cost.cuda()
    trace = torch.zeros_like(cost, dtype=torch.int32)

    dtw_kernel[(1,)](
        cost,
        trace,
        x_skew,
        x_skew.stride(0),
        cost.stride(0),
        trace.stride(0),
        N,
        M,
        BLOCK_SIZE=BLOCK_SIZE
    )

    trace = trace.T.flatten()[:(M + 1) * (M + N + 3)].reshape(M + 1, M + N + 3)[:, :N + 1]
    return backtrace(trace.cpu().numpy())


def dtw(x: torch.Tensor) -> np.ndarray:
    if x.is_cuda:
        try:
            return dtw_cuda(x)
        except (RuntimeError, subprocess.CalledProcessError):
            warnings.warn(
                "Failed to launch Triton kernels, likely due to missing CUDA toolkit; "
                "falling back to a slower DTW implementation..."
            )

    return dtw_cpu(x.double().cpu().numpy())


@dataclass
class WordTiming:
    word: str
    tokens: List[int]
    start: float
    end: float
    probability: float


def find_alignment(
    model: "Whisper",
    tokenizer: Tokenizer,
    text_tokens: List[int],
    mel: torch.Tensor,
    num_frames: int,
    *,
    medfilt_width: int = 7,
    qk_scale: float = 1.0,
) -> List[WordTiming]:
    tokens = torch.tensor(
        [
            *tokenizer.sot_sequence,
            tokenizer.no_timestamps,
            *text_tokens,
            tokenizer.eot,
        ]
    ).to(model.device)

    # install hooks on the cross attention layers to retrieve the attention weights
    QKs = [None] * model.dims.n_text_layer
    hooks = [
        block.cross_attn.register_forward_hook(
            lambda _, ins, outs, index=i: QKs.__setitem__(index, outs[-1][0])
        )
        for i, block in enumerate(model.decoder.blocks)
    ]

    with torch.no_grad():
        logits = model(mel.unsqueeze(0), tokens.unsqueeze(0))[0]
        token_probs = logits[len(tokenizer.sot_sequence):, :tokenizer.eot].softmax(dim=-1)
        text_token_probs = token_probs[np.arange(len(text_tokens)), text_tokens].tolist()

    for hook in hooks:
        hook.remove()

    # heads * tokens * frames
    weights = torch.stack([QKs[l][h] for l, h in model.alignment_heads.indices().T])
    weights = weights[:, :, : num_frames // 2]
    weights = (weights * qk_scale).softmax(dim=-1)
    std, mean = torch.std_mean(weights, dim=-2, keepdim=True, unbiased=False)
    weights = (weights - mean) / std
    weights = median_filter(weights, medfilt_width)

    matrix = weights.mean(axis=0)
    matrix = matrix[len(tokenizer.sot_sequence):-1]
    text_indices, time_indices = dtw(-matrix)

    words, word_tokens = tokenizer.split_to_word_tokens(text_tokens + [tokenizer.eot])
    word_boundaries = np.pad(np.cumsum([len(t) for t in word_tokens[:-1]]), (1, 0))

    jumps = np.pad(np.diff(text_indices), (1, 0), constant_values=1).astype(bool)
    jump_times = time_indices[jumps] / TOKENS_PER_SECOND
    start_times = jump_times[word_boundaries[:-1]]
    end_times = jump_times[word_boundaries[1:]]
    word_probabilities = [
        np.mean(text_token_probs[i:j]) for i, j in zip(word_boundaries[:-1], word_boundaries[1:])
    ]

    # hack: ensure the first and second word is not longer than twice the median word duration.
    # a better segmentation algorithm based on VAD should be able to replace this.
    word_durations = end_times - start_times
    word_durations = word_durations[word_durations.nonzero()]
    if len(word_durations) > 0:
        median_duration = np.median(word_durations)
        max_duration = median_duration * 2
        if len(word_durations) >= 2 and word_durations[1] > max_duration:
            end_times[0] = start_times[1] = max(end_times[2] / 2, end_times[2] - max_duration)
        if len(word_durations) >= 1 and end_times[0] - start_times[0] > max_duration:
            start_times[0] = max(0, end_times[0] - max_duration)

    return [
        WordTiming(word, tokens, start, end, probability)
        for word, tokens, start, end, probability in zip(
            words, word_tokens, start_times, end_times, word_probabilities
        )
    ]


def merge_punctuations(alignment: List[WordTiming], prepended: str, appended: str):
    # merge prepended punctuations
    i = len(alignment) - 2
    j = len(alignment) - 1
    while i >= 0:
        previous = alignment[i]
        following = alignment[j]
        if previous.word.startswith(" ") and previous.word.strip() in prepended:
            # prepend it to the following word
            following.word = previous.word + following.word
            following.tokens = previous.tokens + following.tokens
            previous.word = ""
            previous.tokens = []
        else:
            j = i
        i -= 1

    # merge appended punctuations
    i = 0
    j = 1
    while j < len(alignment):
        previous = alignment[i]
        following = alignment[j]
        if not previous.word.endswith(" ") and following.word in appended:
            # append it to the previous word
            previous.word = previous.word + following.word
            previous.tokens = previous.tokens + following.tokens
            following.word = ""
            following.tokens = []
        else:
            i = j
        j += 1


def add_word_timestamps(
    *,
    segments: List[dict],
    model: "Whisper",
    tokenizer: Tokenizer,
    mel: torch.Tensor,
    num_frames: int,
    prepend_punctuations: str = "\"\'“¿([{-",
    append_punctuations: str = "\"\'.。,，!！?？:：”)]}、",
    **hyperparams,
):
    if len(segments) == 0:
        return

    text_tokens = [t for segment in segments for t in segment["tokens"]]
    alignment = find_alignment(model, tokenizer, text_tokens, mel, num_frames, **hyperparams)
    merge_punctuations(alignment, prepend_punctuations, append_punctuations)

    time_offset = segments[0]["seek"] * HOP_LENGTH / SAMPLE_RATE
    token_sources = np.repeat(np.arange(len(segments)), [len(s["tokens"]) for s in segments])

    for segment in segments:
        segment["words"] = []

    word_boundaries = np.pad(np.cumsum([len(w.tokens) for w in alignment]), (1, 0))
    for i, timing in enumerate(alignment):
        if timing.word:
            segment = segments[token_sources[word_boundaries[i]]]
            start = round(time_offset + timing.start, 2)
            end = round(time_offset + timing.end, 2)
            segment["words"].append(
                dict(word=timing.word, start=start, end=end, probability=timing.probability)
            )

    for segment in segments:
        if len(words := segment["words"]) > 0:
            # adjust the segment-level timestamps based on the word-level timestamps
            segment["start"] = words[0]["start"]
            segment["end"] = words[-1]["end"]
