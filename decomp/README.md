# Roomba Home 3.2.3 decompile notes

Done 2026-10-21, fully offline. Nothing was sent to the robot or iRobot's cloud.

## How the app is built

- **Screens and UI** are Flutter/Dart (Dart 3.12.2), compiled into `libapp.so`. These were analyzed with Blutter, which recovers class and function names, every string, and enum values, plus assembly. It can't recover readable source.
- **All the cloud and MQTT code** is iRobot's own "data SDK", written in Kotlin (`com.irobot.irobotdata.*` and `com.irobot.perimeter.*` in `classes.dex`). The Dart screens call it through Flutter method channels. jadx decompiles this part into nearly readable Java, which makes it the useful half.

## Main finding

roombapy-prime's author already did this exact exercise on app 3.0.0 and 3.2.0 (see `vendor_reference.py` and `vendor_reference.json` in the library). I diffed 3.2.3 against their 3.2.0 extract, and **at the protocol level the two are effectively identical**:

- **Commands:** the same 20 (`dock`, `dryPad`, `evac`, `find`, `fluidRefill`, `pause`, `pointClean`, `reset`, `resume`, `skip`, `spot`, `start`, `startDoNotDisturb`, `stop`, `stopDoNotDisturb`, `stopEvac`, `stopPadDry`, `train`, `washPad`, and `wipe`).
- **Start parameters:** the same (operatingMode, suctionLevel, padWetness, swScrub, twoPass, carpetBoost, edgeOnly, quiet, spray, sealForce, heatedWater, profile, smartCleanId, and others).
- **Writable settings:** the same 27 (audio.volume, autoevacFreq, carpetBoost, childLock, detergent, dryDMode, langs2.*, mapUploadAllowed, name, odoaMode, padDryDur, padWashAllowed, padWetness.padPlate, pwAreaInterval, pwHeat, pwReturn, pwTimeInterval, sanitizationMode, sprayMode, suctionLevel, swScrub, timezone, and twoPass).
- **Kotlin wire formats:** all 223 serializers match. The only new classes are logging/session plumbing.
- **Capability flags:** 42 in total. The library already handles all of them except a few gates it reads under other names or doesn't use (`cap.suctionLvl`, `cap.defCaps`, `digiCap.thresholds`, and `dock.cap.detergent`).
- **Small Dart-side changes in 3.2.3:** zstd map compression, a "recommended threshold" map element (`recommendSill`), a routine-preview entry point, and new Wi-Fi hotspot setup videos. None of these is a new robot command.

So there's no hidden command for the web app to pick up. Anything the official app can make the robot do, the library can already send.

## Files here

- `irobot_sdk_java.zip`: the decompiled Kotlin SDK (jadx). Start in `irobotdata/command`, `irobotdata/robot`, and `irobotdata/maps`.
- `blutter_dart_dump.zip`: Blutter's object pool (`pp.txt`, which holds every string and constant), object dump (`objs.txt`), assembly for the data/settings/device modules, and an IDA names script.
- `enumvals_323.json`: Dart enum values recovered from 3.2.3. Partial, because only enum instances the code references are dumped.
- `ser_323.json`: Kotlin serializer wire keys.
- `caps_323.txt`: capability shadow keys.

## About Ghidra

Ghidra would add little here. The Kotlin half is already readable through jadx. The Dart half needs Blutter's names first, and Blutter only ships an IDA script (in the zip). Ghidra is worth it only for tracing one specific Dart function by hand.

## Reproducing

Blutter (github.com/worawit/blutter) builds the matching Dart VM from GitHub in about 5 minutes. jadx 1.5.1 runs on `base.apk`. The arm64 code is in `split_config.arm64_v8a.apk` inside the .apkm.
