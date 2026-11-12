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
- Wi-Fi turns off by itself while a phone is connected over Bluetooth (fixes choppy Bluetooth music) and comes back on after the phone disconnects
- Bluetooth: one tap to add a phone: Pair, confirm on the phone, and it connects by itself (no more second Pair tap or separate Connect)
- Bluetooth: a phone that drops out reconnects by itself for 2 minutes and the music comes straight back; "Waiting for phone…" is shown instead of "Connected" until the phone really sends audio; one phone at a time; Scan is locked while a phone is connected (scanning made the music choppy)
- Bluetooth: clear message when the audio can't be routed to VB-Cable instead of silent failure
- smoother sound: small audio cushion on both Gigaports, CABLE recorded through WASAPI instead of the old MME driver, Gigaports stay open between reconnects
- the app notices a crashed audio engine or an unplugged Gigaport, shows it, and restarts Live by itself
- volume, vibration, bass, vibration mode, speaker route, zones and Gigaport roles are remembered after a restart
- logs for support in C:\kiosk_launcher\updates\app.log and engine.log
- fix: Bluetooth Live didn't start while the haptics calibration had the Gigaports open
- new look in the Gharieni style: Raleway type, black / warm grey / cream with a bronze accent, hairline sliders and lists (all screens)
- Now Playing: shows which phone is playing; with a phone connected, Pause silences headphones and vibration on the tablet (Windows can't pause or skip the phone itself, so Back/Next are hidden and songs are changed on the phone)
- original colours for the vibration dots on the body
- Settings: Bluetooth name - rename the tablet to "MuVi" (what phones show) with the administrator password, then restart
- Volume, Vibration and Bass depth sliders move in 2 % steps, so a preferred level is easy to set again
- Settings: Wi-Fi moved into Settings; haptics calibration and the Gigaport roles swap moved to Advanced settings, which needs the service password
- two Gigaports again: Gigaport 2 plays the music (CH1-2 headphones, CH3-4 speakers), choose Headphones / Speakers / Both on Now Playing; the Behringer UFO202 is only the AUX input (Settings shows it as "AUX input")
- new pages: Bluetooth (phone list, or the player once a phone is connected), AUX (plays the Behringer AUX input only while this page is open), Programs (Demo as a program card; a program plays only while Programs is open)
- Advanced settings: Haptics character - Dynamics (Muvi / Aurasens original) and Layout (Body zones / Aurasens order) to compare with Aurasens, saved and applied while playing
- Programs: the Aurasens programs (Wellness - Elements, Massage - De-Stress, Bonus, Bespoke experiences, Demo) in categories with artwork, artist and length. They play their own vibration: Gigaport outputs 1-8 get Aurasens channels 1, 3, 14, 16, 23, 24, 27, 28, with the music on the headphones/speakers. The programs are copied onto the tablet separately (C:\kiosk_launcher\programs), not part of the update
