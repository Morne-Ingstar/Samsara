"""Generated PCM only: no microphone, owner audio, filesystem or application."""
import numpy as np
import pytest

from samsara import mic_lab_dsp as dsp

SR = 48000


def noise(seconds=3, rms=.08, seed=1266):
    x = np.random.default_rng(seed).normal(size=int(SR * seconds))
    return (x * rms / np.sqrt(np.mean(x * x))).astype(np.float32)


def sine(hz, seconds=3, amplitude=.15):
    return (amplitude * np.sin(2 * np.pi * hz * np.arange(int(SR * seconds)) / SR)).astype(np.float32)


@pytest.fixture
def clean():
    return noise()


@pytest.fixture
def silence():
    return noise(5, .0005, 93)


def test_clean_judges(clean, silence):
    results = [dsp.noise_floor(silence, SR), dsp.rumble_dc(silence, SR),
               dsp.speech_level(clean, SR), dsp.speech_snr(clean, silence, SR),
               dsp.clipping(clean, SR), dsp.upper_band_ratio(clean, SR),
               dsp.tonal_prominence(silence, SR), dsp.dropouts(clean, SR),
               dsp.clock_drift(SR * 15, SR, 15), dsp.channel_balance(clean, SR)]
    for result in results:
        assert result.verdict == "Good", result
        assert result.value is not None and result.cause


@pytest.mark.parametrize("dbfs,expected", [(-90, "Watch"), (-65, "Good"), (-45, "Watch"), (-30, "Bad")])
def test_noise_levels(dbfs, expected):
    # Band-limited fixture has known speech-band RMS, independent of filter implementation.
    x = sine(1000, 5, np.sqrt(2) * 10 ** (dbfs / 20))
    result = dsp.noise_floor(x, SR)
    assert result.verdict == expected
    assert result.value == pytest.approx(dbfs, abs=.1)


def test_noise_middle_and_digital_gate(silence):
    expected = dsp.noise_floor(silence, SR).value
    silence[:SR // 2] = .8
    silence[-SR // 2:] = .8
    assert dsp.noise_floor(silence, SR).value == pytest.approx(expected, abs=.1)
    gated = dsp.noise_floor(np.zeros(SR, np.float32), SR)
    assert gated.verdict == "Watch" and gated.details["gated"] == [True]


def test_hum_rumble_dc(silence):
    hum = silence + sine(60, 5, .03)
    assert dsp.rumble_dc(hum, SR).verdict == "Bad"
    assert dsp.rumble_dc(silence + .03, SR).verdict == "Bad"
    assert dsp.rumble_dc(silence * 10 + .003, SR).verdict == "Watch"
    result = dsp.tonal_prominence(hum, SR)
    assert result.verdict == "Bad"
    assert result.value["frequency_hz"] == 60


@pytest.mark.parametrize("amplitude,expected", [(.0002, "Watch"), (.005, "Bad"), (.03, "Watch"), (.15, "Good"), (.5, "Watch"), (.9, "Bad")])
def test_voiced_gain(amplitude, expected):
    assert dsp.speech_level(sine(997, amplitude=amplitude), SR).verdict == expected


def test_voicing_ignores_pauses():
    phrase = sine(997)
    x = np.r_[np.zeros(SR, np.float32), phrase, np.zeros(SR, np.float32)]
    assert dsp.speech_level(x, SR).value == pytest.approx(dsp.speech_level(phrase, SR).value, abs=.1)


@pytest.mark.parametrize("snr,expected", [(10, "Bad"), (20, "Watch"), (30, "Good")])
def test_snr(snr, expected):
    speech = sine(997, amplitude=.1)
    silence = sine(997, 5, .1 / 10 ** (snr / 20))
    result = dsp.speech_snr(speech, silence, SR)
    assert result.verdict == expected
    assert result.value == pytest.approx(snr, abs=.1)


def test_gated_snr_cannot_pass(clean):
    assert dsp.speech_snr(clean, np.zeros(SR, np.float32), SR).value is None


def test_half_scale_flat_tops_and_full_scale(clean):
    half_square = (.5 * np.sign(sine(997))).astype(np.float32)
    clipped = dsp.clipping(half_square, SR)
    assert clipped.verdict == "Bad"
    assert clipped.value["full_scale_samples"] == 0
    assert clipped.value["flat_top_runs"] > 3
    clean[:21] = 1.
    assert dsp.clipping(clean, SR).verdict == "Bad"


@pytest.mark.parametrize("count,expected", [(1, "Watch"), (3, "Watch"), (4, "Bad")])
def test_flat_top_run_count(clean, count, expected):
    for index in range(count):
        clean[1000 * (index + 1):1000 * (index + 1) + 8] = .5
    result = dsp.clipping(clean, SR)
    assert result.value["flat_top_runs"] == count
    assert result.verdict == expected


def test_lowpassed_fricatives(clean):
    spectrum = np.fft.rfft(clean)
    spectrum[np.fft.rfftfreq(len(clean), 1 / SR) > 3800] = 0
    narrow = np.fft.irfft(spectrum, n=len(clean)).astype(np.float32)
    assert dsp.upper_band_ratio(narrow, SR).verdict == "Bad"
    assert dsp.upper_band_ratio(clean, 8000).value is None
    assert dsp.upper_band_ratio(clean, 16000).verdict == "Good"


@pytest.mark.parametrize("hz", [50, 60, 120, 180, 1000, 2000, 3000, 4000])
def test_tonal_whine(silence, hz):
    result = dsp.tonal_prominence(silence + sine(hz, 5, .003), SR)
    assert result.verdict == "Bad"
    assert result.value["frequency_hz"] == hz


def agc_fixture(phrase_factor=1., pause_factor=1.):
    phrase = noise(1, .04)
    pause = noise(1, .0005)
    burst = sine(997, 1, .8)
    x = np.concatenate([phrase, burst, pause, pause * pause_factor, phrase * phrase_factor])
    ranges = dict(baseline=(0, SR), quiet_after=(4 * SR, 5 * SR), pause_start=(2 * SR, 3 * SR), pause_end=(3 * SR, 4 * SR))
    return x, ranges


@pytest.mark.parametrize("phrase,pause,expected", [(1, 1, "Good"), (.6, 1, "Watch"), (.25, 1, "Bad"), (1, 4, "Bad")])
def test_agc(phrase, pause, expected):
    x, ranges = agc_fixture(phrase, pause)
    assert dsp.agc_pumping(x, SR, **ranges).verdict == expected


@pytest.mark.parametrize("milliseconds,count,expected", [(1, 1, "Good"), (2, 1, "Watch"), (4, 1, "Watch"), (5, 1, "Bad"), (3, 2, "Bad")])
def test_zero_dropouts(clean, milliseconds, count, expected):
    for i in range(count):
        start = (i + 1) * SR // 2
        clean[start:start + int(milliseconds * SR / 1000)] = 0
    result = dsp.dropouts(clean, SR)
    assert result.verdict == expected
    assert result.value["zero_runs"] == (count if milliseconds >= 2 else 0)


def test_edges_not_dropouts_and_overflow(clean):
    clean[:SR // 10] = clean[-SR // 10:] = 0
    assert dsp.dropouts(clean, SR).verdict == "Good"
    assert dsp.dropouts(clean, SR, input_overflows=1).verdict == "Bad"


@pytest.mark.parametrize("factor,expected", [(1., "Good"), (1.003, "Watch"), (1.01, "Bad"), (.99, "Bad")])
def test_clock_drift(factor, expected):
    result = dsp.clock_drift(round(SR * 15 * factor), SR, 15)
    assert result.verdict == expected
    assert result.value == pytest.approx(100 * (factor - 1))


@pytest.mark.parametrize("delay,expected", [(.05, "Good"), (.2, "Watch"), (.4, "Bad")])
def test_onset(delay, expected):
    x = np.r_[np.zeros(int(SR * delay), np.float32), sine(997, 1)]
    result = dsp.onset_delay(x, SR, noise_dbfs=-65)
    assert result.verdict == expected
    assert result.value == pytest.approx(delay, abs=.01)


def test_boundary_and_trigger():
    x = np.r_[np.zeros(SR // 2, np.float32), sine(997, 1), np.zeros(SR // 2, np.float32)]
    assert dsp.boundary_margin(x, SR, cut_range=(SR // 2, 3 * SR // 2), noise_dbfs=-65).verdict == "Good"
    assert dsp.boundary_margin(x, SR, cut_range=(SR // 2 + 1000, 3 * SR // 2 - 1000), noise_dbfs=-65).verdict == "Bad"
    assert dsp.onset_delay(x, SR, noise_dbfs=-65, trigger_sample=SR // 2).verdict == "Good"


def test_stereo_does_not_hide_dead_channel(clean, silence):
    live = np.column_stack([clean, -clean])  # Downmix would cancel this.
    dead = np.column_stack([clean, np.zeros_like(clean)])
    assert dsp.channel_balance(live, SR).verdict == "Good"
    assert dsp.channel_balance(dead, SR).verdict == "Bad"
    assert dsp.speech_level(live, SR).verdict == "Good"
    assert dsp.speech_level(dead, SR).verdict == "Watch"
    assert dsp.noise_floor(np.column_stack([silence, np.zeros_like(silence)]), SR).verdict == "Watch"
    assert dsp.upper_band_ratio(live, SR).verdict == "Good"


@pytest.mark.parametrize("judge", [dsp.noise_floor, dsp.rumble_dc, dsp.speech_level, dsp.clipping, dsp.upper_band_ratio, dsp.tonal_prominence, dsp.dropouts, dsp.channel_balance])
def test_insufficient(judge):
    assert judge(np.empty(0, np.float32), SR).verdict == "Watch"


@pytest.mark.parametrize("bad", [np.array([np.nan], np.float32), np.ones(10, np.int16), np.ones((10, 3), np.float32)])
def test_invalid_pcm(bad):
    with pytest.raises(ValueError):
        dsp.noise_floor(bad, SR)


def test_invalid_ranges_and_clock(clean):
    with pytest.raises(ValueError):
        dsp.boundary_margin(clean, SR, cut_range=(10, 1), noise_dbfs=-65)
    with pytest.raises(ValueError):
        dsp.clock_drift(SR, SR, 0)
    with pytest.raises(ValueError):
        dsp.noise_floor(clean, 0)
    with pytest.raises(ValueError):
        dsp.speech_snr(clean, np.column_stack([clean, clean]), SR)


def test_judges_do_not_mutate(clean):
    saved = clean.copy()
    for judge in (dsp.noise_floor, dsp.rumble_dc, dsp.speech_level, dsp.clipping, dsp.upper_band_ratio, dsp.tonal_prominence, dsp.dropouts):
        judge(clean, SR)
    np.testing.assert_array_equal(clean, saved)
