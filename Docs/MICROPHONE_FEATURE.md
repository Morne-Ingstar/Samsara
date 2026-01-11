# Microphone Selection Feature

## What's New

Your dictation app now supports microphone selection in two ways:

### 1. Settings Window
- Open Settings from the system tray
- Find the new "Microphone" dropdown under "Other Settings"
- Select your preferred microphone from the list
- Click Save to apply

### 2. System Tray Quick Switch
- Right-click the dictation app icon in your system tray
- The first menu item shows your current microphone (🎤 icon)
- Hover over it to see a submenu with all available microphones
- Click any microphone to instantly switch to it
- The active microphone has a checkmark (✓)

## How It Works

- The app automatically detects all available input devices on your system
- Your microphone preference is saved to `config.json`
- Switching microphones stops any active recording/continuous mode
- You can switch mics on the fly without restarting the app

## Technical Details

New config option:
```json
{
  "microphone": null  // null = default, or device ID number
}
```

The microphone ID is the device index from sounddevice. The app stores this and restores it on startup.

## Troubleshooting

If you don't see your microphone:
1. Make sure it's plugged in and enabled in Windows Sound Settings
2. Restart the dictation app
3. Check that the microphone is set as an input device (not just output)

The app only shows devices with input channels available.
