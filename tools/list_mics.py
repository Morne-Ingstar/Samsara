import sounddevice as sd

hostapis = sd.query_hostapis()

print("=== ALL INPUT DEVICES WITH FULL DETAILS ===\n")
for i, d in enumerate(sd.query_devices()):
    if d['max_input_channels'] > 0:
        api_name = hostapis[d['hostapi']]['name']
        print(f"Index {i}:")
        print(f"  Name:        {d['name']}")
        print(f"  Host API:    {api_name}")
        print(f"  Channels:    {d['max_input_channels']}")
        print(f"  Default SR:  {d['default_samplerate']}")
        print(f"  Low latency: {d['default_low_input_latency']:.4f}s")
        print(f"  High latency:{d['default_high_input_latency']:.4f}s")
        print()

print("\n=== WASAPI DEVICES ONLY ===\n")
wasapi_idx = None
for idx, api in enumerate(hostapis):
    if 'WASAPI' in api['name']:
        wasapi_idx = idx
        break

if wasapi_idx is not None:
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0 and d['hostapi'] == wasapi_idx:
            print(f"Index {i}: {d['name']}")
            print(f"  Default SR: {d['default_samplerate']}")
            print(f"  Channels:   {d['max_input_channels']}")

            # Test which sample rates work
            for sr in [16000, 22050, 44100, 48000]:
                try:
                    sd.check_input_settings(device=i, samplerate=sr, channels=1)
                    print(f"  {sr}Hz: OK")
                except Exception as e:
                    print(f"  {sr}Hz: FAIL ({e})")
            print()
