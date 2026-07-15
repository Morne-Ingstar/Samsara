from types import SimpleNamespace

from samsara.output_devices import (
    enumerate_output_devices,
    output_sample_rate,
    reconcile_output_device,
)


def test_enumeration_deduplicates_host_apis_and_prefers_wasapi():
    fake_sd = SimpleNamespace(
        query_hostapis=lambda: [
            {"name": "Windows WASAPI"},
            {"name": "MME"},
        ],
        query_devices=lambda: [
            {"name": "Speakers", "max_output_channels": 2, "hostapi": 1},
            {"name": "Speakers", "max_output_channels": 2, "hostapi": 0},
            {"name": "Microphone", "max_output_channels": 0, "hostapi": 0},
        ],
    )

    assert enumerate_output_devices(fake_sd) == [
        {"id": 1, "name": "Speakers", "hostapi": "Windows WASAPI"}
    ]


def test_default_hides_raw_non_wasapi_driver_endpoints():
    fake_sd = SimpleNamespace(
        query_hostapis=lambda: [
            {"name": "Windows WASAPI"},
            {"name": "Windows WDM-KS"},
            {"name": "MME"},
        ],
        query_devices=lambda: [
            {
                "name": "Headphones (SteelSeries Sonar - Media)",
                "max_output_channels": 2,
                "hostapi": 0,
            },
            {
                "name": "Headphones (SteelSeries_Sonar_VAD Media Wave Speaker)",
                "max_output_channels": 2,
                "hostapi": 1,
            },
            {
                "name": "Microsoft Sound Mapper - Output",
                "max_output_channels": 2,
                "hostapi": 2,
            },
        ],
    )

    assert enumerate_output_devices(fake_sd) == [
        {
            "id": 0,
            "name": "Headphones (SteelSeries Sonar - Media)",
            "hostapi": "Windows WASAPI",
        }
    ]


def test_show_all_audio_devices_keeps_non_wasapi_endpoints():
    fake_sd = SimpleNamespace(
        query_hostapis=lambda: [
            {"name": "Windows WASAPI"},
            {"name": "MME"},
        ],
        query_devices=lambda: [
            {"name": "Speakers", "max_output_channels": 2, "hostapi": 0},
            {"name": "Microsoft Sound Mapper - Output", "max_output_channels": 2, "hostapi": 1},
        ],
    )

    assert {item["name"] for item in enumerate_output_devices(fake_sd, show_all=True)} == {
        "Speakers",
        "Microsoft Sound Mapper - Output",
    }


def test_reconciliation_tracks_name_when_portaudio_index_changes():
    devices = [{"id": 9, "name": "USB Headset", "hostapi": "Windows WASAPI"}]

    assert reconcile_output_device(devices, 2, "USB Headset") == (
        9,
        "USB Headset",
        False,
    )


def test_reconciliation_recovers_unique_mme_truncated_name_as_wasapi():
    devices = [
        {
            "id": 23,
            "name": "Headphones (Arctis Nova Pro Wireless)",
            "hostapi": "Windows WASAPI",
        }
    ]

    assert reconcile_output_device(
        devices, 7, "Headphones (Arctis Nova Pro Wir"
    ) == (23, "Headphones (Arctis Nova Pro Wireless)", False)


def test_reconciliation_does_not_guess_ambiguous_truncated_name():
    devices = [
        {"id": 23, "name": "Headphones (Arctis Nova Pro Wireless)", "hostapi": "Windows WASAPI"},
        {"id": 24, "name": "Headphones (Arctis Nova Pro Wired)", "hostapi": "Windows WASAPI"},
    ]

    assert reconcile_output_device(
        devices, 7, "Headphones (Arctis Nova Pro Wir"
    ) == (None, None, True)


def test_reconciliation_does_not_prefix_match_short_generic_names():
    devices = [
        {"id": 4, "name": "Speakers (Realtek Audio)", "hostapi": "Windows WASAPI"},
    ]

    assert reconcile_output_device(devices, 2, "Speakers") == (None, None, True)


def test_missing_explicit_output_falls_back_without_losing_saved_identity():
    assert reconcile_output_device([], 2, "USB Headset") == (None, None, True)
    assert reconcile_output_device([], None, None) == (None, None, False)


def test_output_sample_rate_uses_selected_endpoint_mix_format():
    calls = []

    def query_devices(device, kind):
        calls.append((device, kind))
        return {"default_samplerate": 48000.0}

    assert output_sample_rate(
        SimpleNamespace(query_devices=query_devices), 23,
    ) == 48000
    assert calls == [(23, "output")]


def test_output_sample_rate_queries_default_for_none_and_safely_falls_back():
    default_sd = SimpleNamespace(
        query_devices=lambda device, kind: {"default_samplerate": 96000},
    )
    broken_sd = SimpleNamespace(
        query_devices=lambda *_args: (_ for _ in ()).throw(RuntimeError("gone")),
    )

    assert output_sample_rate(default_sd, None) == 96000
    assert output_sample_rate(broken_sd, 23, fallback=44100) == 44100
