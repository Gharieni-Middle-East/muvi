# Version History Muvi

## v1.0.1
- Bluetooth: scan, pair and connect phones; phone audio plays through the Gigaport with vibration
- AUX input: phone into the Behringer USB interface, played through the Gigaport
- Demo tab: test track with the same volume / vibration / bass controls
- vibration split over 8 shakers, bed visualization with live shaker levels
- Settings: hardware status (vibration Gigaport, headphones, battery)

## v1.0.2
- haptics vibration mode, fix Gigaport connection
- add online update from Settings: check, download, install and restart (rolls back if the new version doesn't start)
- add Wi-Fi screen: scan, connect (password), disconnect
- restart the app automatically if it crashes or closes (kiosk tablets)
- haptics now match the Raspberry Pi app: no rattle when Windows runs at 48 kHz, soft limiting instead of hard clipping, 8 separate channels on dual ASIO4ALL
- add haptics calibration in Settings: level, on/off and body position per transducer, overall haptic level, step-by-step test that pulses one transducer at a time
- add Wi-Fi on/off switch on the Wi-Fi screen (to test whether Wi-Fi disturbs the Bluetooth audio)
