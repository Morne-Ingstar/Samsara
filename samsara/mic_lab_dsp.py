"""Offline Mic Lab judges. No I/O, capture, Qt, or learned VAD.

Audio is finite floating-point PCM in (frames,) or (frames, 1|2) shape.
Channels are judged separately: the worst verdict wins. Invalid arguments raise
ValueError; insufficient evidence returns Watch with value=None. Thresholds are
diagnostic starting points, not calibrated hardware certification standards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
from scipy import signal

# Carbon mic-lab review 2026-11-18 section 2 unless explicitly marked 1266.
# Every policy/timing number used by a judge is collected here with its source.
THRESHOLDS = {
    "noise_dbfs": (-85., -50., -40., "Carbon section 2: gate/Good/Watch"),
    "rumble_db": (6., 12., "Carbon section 2"),
    "dc_dbfs": (-54., -40., "Carbon section 2; intervening range Watch"),
    "speech_dbfs": (-38., -28., -12., -6., "Carbon section 2"),
    "snr_db": (15., 25., "Carbon section 2"),
    "clip": (.999, 1e-5, 3, .25, 3, 20, 3, -18., -1., "Carbon section 2"),
    "upper_db": (-35., -20., "Carbon section 2"),
    "tone_db": (12., 20., "Carbon section 2"),
    "agc_db": (3., 6., "Carbon section 2"),
    "dropout": (.002, .005, -35., .010, "Carbon section 2; 10 ms flank: 1266 heuristic"),
    "drift_pct": (.1, .5, "Carbon section 6: >0.5% flag; 0.1% Watch: 1266 heuristic"),
    "onset_s": (.150, .300, "1266 provisional diagnostic thresholds; human timing also contributes"),
    "boundary": (.040, 6., "Carbon section 2: 40 ms outside cut, noise +6 dB"),
    "onset_above_noise_db": (10., "Carbon section 2"),
    "silence_window_s": (.100, .5, .1, "Carbon section 2: 100 ms; trim 0.5 s/edge of 5 s; 1266: scale trim to 10% for shorter takes"),
    "voicing": (.030, -50., 30., 10., "1266 energy rule: 30 ms, max(-50, peak-30, optional noise+10) dBFS"),
    "speech_band_hz": (100., 7000., "Carbon section 2"),
    "upper_bands_hz": (4200., 7500., 500., 3500., .85, "Carbon section 2: top 15% ZCR"),
    "tone_hz": (50., 60., 120., 180., 1000., 2000., 3000., 4000., "Carbon section 2"),
    "tone_analysis": (2., 4., 30., 1., "Carbon ~2 Hz PSD; 1266 +/-4 Hz peak, +/-30 Hz local median, >=1 s"),
    "channel_delta_db": (20., "Carbon revised battery step 0"),
    "analysis_frame_s": (.010, "1266 onset/boundary frame resolution"),
}
_RANK = {"Good": 0, "Watch": 1, "Bad": 2}


@dataclass(frozen=True)
class Measurement:
    value: object
    verdict: str
    cause: str
    details: dict = field(default_factory=dict)


def _audio(audio, sample_rate):
    x = np.asarray(audio)
    if (x.dtype.kind != "f" or x.ndim not in (1, 2) or
            (x.ndim == 2 and x.shape[1] not in (1, 2)) or
            not np.all(np.isfinite(x))):
        raise ValueError("audio must be finite floating-point mono or stereo PCM")
    if not np.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be positive and finite")
    return x.astype(np.float64, copy=False).reshape(len(x), -1) if len(x) else np.empty((0, 1 if x.ndim == 1 else x.shape[1]))


def _db(power):
    return 10 * np.log10(np.maximum(power, 1e-30))


def _rms_db(x):
    return _db(np.mean(x * x, axis=0))


def _unknown(cause):
    return Measurement(None, "Watch", cause)


def _result(values, verdicts, cause, **details):
    verdict = max(verdicts, key=_RANK.get)
    return Measurement(values[0] if len(values) == 1 else values, verdict, cause, details)


def _frames(x, sr, seconds):
    n = max(1, int(round(sr * seconds)))
    count = len(x) // n
    return x[:count * n].reshape(count, n, x.shape[1])


def _band(x, sr, low=None, high=None):
    low = THRESHOLDS["speech_band_hz"][0] if low is None else low
    high = THRESHOLDS["speech_band_hz"][1] if high is None else high
    # Preserve the requested passband; never silently lower a cutoff to Nyquist.
    if sr <= 2 * high or len(x) < 64:
        return None
    sos = signal.butter(4, [low, high], fs=sr, btype="bandpass", output="sos")
    return signal.sosfiltfilt(sos, x, axis=0)


def noise_floor(audio, sample_rate):
    """P50 speech-band RMS in complete 100 ms windows, central 80% (max trim .5 s/edge)."""
    x = _audio(audio, sample_rate)
    duration, edge = THRESHOLDS["silence_window_s"][:2]
    trim = min(int(edge * sample_rate), int(len(x) * THRESHOLDS["silence_window_s"][2]))
    x = x[trim:len(x) - trim] if trim else x
    y = _band(x, sample_rate)
    if y is None or len(y) < duration * sample_rate:
        return _unknown("A complete 100 ms silence window and sample rate above 14 kHz are required")
    levels = np.median(_db(np.mean(_frames(y, sample_rate, duration) ** 2, axis=1)), axis=0)
    gate, good, watch = THRESHOLDS["noise_dbfs"][:3]
    verdicts = ["Watch" if v < gate else "Good" if v <= good else "Watch" if v <= watch else "Bad" for v in levels]
    return _result(levels.tolist(), verdicts, "Silence speech-band level; below -85 dBFS suggests a gate, mute, or dead channel", gated=(levels < gate).tolist())


def rumble_dc(audio, sample_rate):
    x = _audio(audio, sample_rate)
    cutoff = THRESHOLDS["speech_band_hz"][0]
    if len(x) < max(64, sample_rate * THRESHOLDS["silence_window_s"][0]) or sample_rate <= 2 * cutoff:
        return _unknown("At least 100 ms of silence above 200 Hz sample rate is required")
    hp = signal.sosfiltfilt(signal.butter(4, cutoff, fs=sample_rate, btype="highpass", output="sos"), x, axis=0)
    delta = np.maximum(0, _rms_db(x) - _rms_db(hp))
    dc = _db(np.mean(x, axis=0) ** 2)
    good, bad = THRESHOLDS["rumble_db"][:2]
    dc_good, dc_bad = THRESHOLDS["dc_dbfs"][:2]
    verdicts = ["Bad" if d > bad or c > dc_bad else "Watch" if d >= good or c >= dc_good else "Good" for d, c in zip(delta, dc)]
    return _result([dict(rumble_db=float(d), dc_dbfs=float(c)) for d, c in zip(delta, dc)], verdicts, "Low-frequency energy or DC bias can indicate vibration or ADC offset")


def _voiced(x, sr, noise_dbfs=None):
    y = _band(x, sr)
    if y is None:
        return None
    seconds, floor, relative, above_noise = THRESHOLDS["voicing"][:4]
    frames = _frames(y, sr, seconds)
    if not len(frames):
        return None
    levels = _db(np.mean(frames ** 2, axis=1))
    threshold = np.maximum(floor, levels.max(axis=0) - relative)
    if noise_dbfs is not None:
        threshold = np.maximum(threshold, np.asarray(noise_dbfs) + above_noise)
    return frames, levels, levels >= threshold


def speech_level(audio, sample_rate, *, noise_dbfs=None):
    """P50 30 ms speech-band energy frames above max(-50, peak-30, noise+10)."""
    data = _voiced(_audio(audio, sample_rate), sample_rate, noise_dbfs)
    if data is None or not data[2].any(axis=0).all():
        return _unknown("No voiced energy on at least one channel; check signal or gate")
    _, levels, mask = data
    values = [float(np.median(levels[mask[:, c], c])) for c in range(levels.shape[1])]
    low, good_low, good_high, high = THRESHOLDS["speech_dbfs"][:4]
    return _result(values, ["Bad" if v < low or v > high else "Good" if good_low <= v <= good_high else "Watch" for v in values], "Voiced energy level indicates gain headroom; energy gating is not a speech classifier")


def speech_snr(speech, silence, sample_rate):
    x, n = _audio(speech, sample_rate), _audio(silence, sample_rate)
    if x.shape[1] != n.shape[1]:
        raise ValueError("speech and silence channel counts must match")
    floor = noise_floor(silence, sample_rate)
    if floor.value is None or any(floor.details["gated"]):
        return _unknown("Silence is missing or gated; SNR would be misleading")
    active = speech_level(speech, sample_rate, noise_dbfs=floor.value)
    if active.value is None:
        return _unknown(active.cause)
    values = np.atleast_1d(active.value) - np.atleast_1d(floor.value)
    bad, good = THRESHOLDS["snr_db"][:2]
    return _result(values.tolist(), ["Good" if v >= good else "Watch" if v >= bad else "Bad" for v in values], "Voiced speech-band level minus the matched silence floor")


def _runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def clipping(audio, sample_rate):
    x = _audio(audio, sample_rate)
    if not len(x):
        return _unknown("No samples to inspect for clipping")
    fs, eps, minimum, fraction, fs_watch, fs_bad, run_watch, peak_low, peak_high = THRESHOLDS["clip"][:9]
    values, verdicts = [], []
    for channel in x.T:
        peak = float(np.max(np.abs(channel)))
        equal = (np.abs(np.diff(channel)) <= eps) & (np.abs(channel[:-1]) >= fraction * peak) & (np.abs(channel[1:]) >= fraction * peak)
        runs = [(int(a), int(b + 1)) for a, b in _runs(equal) if b - a + 1 >= minimum] if peak > eps else []
        full_scale = int(np.sum(np.abs(channel) >= fs))
        peak_db = float(_db(peak ** 2))
        verdict = "Bad" if len(runs) > run_watch or full_scale > fs_bad else "Watch" if runs or full_scale >= fs_watch or peak_db > peak_high or peak_db < peak_low else "Good"
        values.append(dict(full_scale_samples=full_scale, flat_top_runs=len(runs), peak_dbfs=peak_db, runs=runs))
        verdicts.append(verdict)
    return _result(values, verdicts, "Repeated plateaus expose clipping even after Windows scales the signal down")


def upper_band_ratio(audio, sample_rate):
    x = _audio(audio, sample_rate)
    upper_low, upper_high, mid_low, mid_high, quantile = THRESHOLDS["upper_bands_hz"][:5]
    if sample_rate <= 2 * upper_high:
        return _unknown("The full 7.5 kHz upper band is unavailable at this sample rate")
    data = _voiced(x, sample_rate)
    if data is None or not data[2].any(axis=0).all():
        return _unknown("No voiced frames for the fricative bandwidth check")
    # Select with speech-band voicing, measure the original native-rate frames.
    frames = _frames(x, sample_rate, THRESHOLDS["voicing"][0])
    mask = data[2]
    zcr = np.mean(np.diff(np.signbit(frames), axis=1), axis=1)
    frequency = np.fft.rfftfreq(frames.shape[1], 1 / sample_rate)
    psd = np.abs(np.fft.rfft(frames * np.hanning(frames.shape[1])[None, :, None], axis=1)) ** 2
    values = []
    for c in range(x.shape[1]):
        selected = mask[:, c] & (zcr[:, c] >= np.quantile(zcr[mask[:, c], c], quantile))
        power = psd[selected, :, c].mean(axis=0)
        up = power[(frequency >= upper_low) & (frequency <= upper_high)].sum()
        mid = power[(frequency >= mid_low) & (frequency <= mid_high)].sum()
        values.append(float(_db(up) - _db(mid)))
    bad, good = THRESHOLDS["upper_db"][:2]
    return _result(values, ["Good" if v >= good else "Watch" if v >= bad else "Bad" for v in values], "Weak fricative high band suggests narrowband transport or suppression; confirm with endpoint identity")


def tonal_prominence(audio, sample_rate):
    x = _audio(audio, sample_rate)
    resolution, peak_width, local_width, seconds = THRESHOLDS["tone_analysis"][:4]
    if len(x) < sample_rate * seconds or sample_rate <= 2 * (4000 + local_width):
        return _unknown("At least one second and the full 4 kHz neighborhood are required")
    f, psd = signal.welch(x, fs=sample_rate, nperseg=int(round(sample_rate / resolution)), axis=0)
    values, verdicts = [], []
    good, bad = THRESHOLDS["tone_db"][:2]
    for c in range(x.shape[1]):
        scores = {}
        for hz in THRESHOLDS["tone_hz"][:-1]:
            distance = np.abs(f - hz)
            peak = psd[distance <= peak_width, c].max()
            background = np.median(psd[(distance > peak_width) & (distance <= local_width), c])
            scores[int(hz)] = float(_db(peak) - _db(background))
        hz = max(scores, key=scores.get)
        prominence = scores[hz]
        values.append(dict(prominence_db=prominence, frequency_hz=hz, tones_db=scores))
        verdicts.append("Good" if prominence < good else "Watch" if prominence <= bad else "Bad")
    return _result(values, verdicts, "A narrow spectral spike can indicate mains hum or USB whine")


def _range(x, region):
    if len(region) != 2 or any(not isinstance(v, (int, np.integer)) for v in region):
        raise ValueError("ranges must contain two integer sample indices")
    a, b = region
    if not 0 <= a < b <= len(x):
        raise ValueError("sample range is empty or outside audio")
    return x[a:b]


def agc_pumping(audio, sample_rate, *, baseline, quiet_after, pause_start, pause_end):
    """Compare matched quiet phrases and early/late pause ranges (sample indices).

Caller supplies equivalent baseline/post-burst phrases and pause ranges away
from the burst's reverberation. A measured shift alone cannot prove AGC.
"""
    x = _audio(audio, sample_rate)
    phrases = [speech_level(_range(x, r), sample_rate) for r in (baseline, quiet_after)]
    pauses = [noise_floor(_range(x, r), sample_rate) for r in (pause_start, pause_end)]
    if any(m.value is None for m in phrases + pauses) or any(any(m.details["gated"]) for m in pauses):
        return _unknown("Matched phrases and ungated pause windows are required for AGC comparison")
    shift = np.atleast_1d(phrases[1].value) - np.atleast_1d(phrases[0].value)
    rise = np.atleast_1d(pauses[1].value) - np.atleast_1d(pauses[0].value)
    good, bad = THRESHOLDS["agc_db"][:2]
    magnitude = np.maximum(np.abs(shift), np.maximum(rise, 0))
    return _result([dict(phrase_shift_db=float(s), pause_rise_db=float(r)) for s, r in zip(shift, rise)], ["Good" if v < good else "Watch" if v <= bad else "Bad" for v in magnitude], "Post-burst phrase change or rising pause noise suggests gain pumping")


def dropouts(audio, sample_rate, *, input_overflows=0):
    x = _audio(audio, sample_rate)
    if not isinstance(input_overflows, (int, np.integer)) or input_overflows < 0:
        raise ValueError("input_overflows must be a nonnegative integer")
    minimum, long_gap, flank_db, flank_s = THRESHOLDS["dropout"][:4]
    flank = max(1, int(round(flank_s * sample_rate)))
    if len(x) < 2 * flank:
        return _unknown("More audio is required to check both speech flanks")
    values, verdicts = [], []
    for channel in x.T:
        runs = [(int(a), int(b)) for a, b in _runs(channel == 0) if (b - a) / sample_rate >= minimum and a >= flank and b + flank <= len(x) and _rms_db(channel[a - flank:a]) > flank_db and _rms_db(channel[b:b + flank]) > flank_db]
        longest = max(((b - a) / sample_rate for a, b in runs), default=0.)
        verdicts.append("Bad" if input_overflows or len(runs) >= 2 or longest >= long_gap else "Watch" if runs else "Good")
        values.append(dict(zero_runs=len(runs), longest_s=longest, input_overflows=int(input_overflows), runs=runs))
    return _result(values, verdicts, "Exact-zero gaps flanked by speech indicate lost audio; leading/trailing silence is excluded")


def clock_drift(frames_captured, sample_rate, wall_seconds):
    if (not isinstance(frames_captured, (int, np.integer)) or frames_captured < 0 or
            not np.isfinite(sample_rate) or sample_rate <= 0 or not np.isfinite(wall_seconds) or wall_seconds <= 0):
        raise ValueError("nonnegative frame count and positive finite rate/time required")
    value = 100 * (frames_captured / (sample_rate * wall_seconds) - 1)
    good, bad = THRESHOLDS["drift_pct"][:2]
    return Measurement(value, "Good" if abs(value) <= good else "Watch" if abs(value) <= bad else "Bad", "Capture duration differs from wall time; align timing to stream start/stop before blaming the clock")


def onset_delay(audio, sample_rate, *, noise_dbfs, trigger_sample=0):
    x = _audio(audio, sample_rate)
    if not isinstance(trigger_sample, (int, np.integer)) or not 0 <= trigger_sample < len(x) or not np.all(np.isfinite(noise_dbfs)):
        raise ValueError("valid trigger index and finite noise floor required")
    size = max(1, int(round(sample_rate * THRESHOLDS["analysis_frame_s"][0])))
    levels = _db(np.mean(_frames(x[trigger_sample:], sample_rate, size / sample_rate) ** 2, axis=1))
    values, verdicts = [], []
    good, bad = THRESHOLDS["onset_s"][:2]
    thresholds = np.broadcast_to(np.asarray(noise_dbfs), (x.shape[1],)) + THRESHOLDS["onset_above_noise_db"][0]
    for c in range(x.shape[1]):
        hits = np.flatnonzero(levels[:, c] > thresholds[c])
        if not len(hits):
            return _unknown("No acoustic onset above noise +10 dB on at least one channel")
        delay = float(hits[0] * size / sample_rate)
        values.append(delay)
        verdicts.append("Good" if delay <= good else "Watch" if delay <= bad else "Bad")
    return _result(values, verdicts, "Acoustic delay includes human timing; it does not measure decoder latency")


def boundary_margin(audio, sample_rate, *, cut_range, noise_dbfs):
    x = _audio(audio, sample_rate)
    _range(x, cut_range)
    if not np.all(np.isfinite(noise_dbfs)):
        raise ValueError("finite noise floor required")
    seconds, above = THRESHOLDS["boundary"][:2]
    width = max(1, int(round(seconds * sample_rate)))
    a, b = cut_range
    outside = [x[max(0, a - width):a], x[b:min(len(x), b + width)]]
    values = []
    thresholds = np.broadcast_to(np.asarray(noise_dbfs), (x.shape[1],)) + above
    for c in range(x.shape[1]):
        flags = []
        for chunk in outside:
            frames = _frames(chunk, sample_rate, THRESHOLDS["analysis_frame_s"][0])
            flags.append(bool(len(frames) and np.any(_db(np.mean(frames[:, :, c] ** 2, axis=1)) > thresholds[c])))
        values.append(dict(before=flags[0], after=flags[1]))
    return _result(values, ["Bad" if v["before"] or v["after"] else "Good" for v in values], "Energy outside the cut may indicate lost speech; transcript evidence is needed to attribute a lost word")


def channel_balance(audio, sample_rate):
    x = _audio(audio, sample_rate)
    if not len(x):
        return _unknown("No audio to compare channels")
    levels = _rms_db(x)
    delta = float(np.ptp(levels))
    return Measurement(delta, "Bad" if delta >= THRESHOLDS["channel_delta_db"][0] else "Good", "Channel level difference exposes dead or unconnected stereo inputs", {"channel_dbfs": levels.tolist()})
