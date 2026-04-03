# Polar H10 Monitor

Ein minimalistisches, schwebendes Echtzeit-Fenster für den **Polar H10 Herzfrequenzsensor** unter **Linux mit KDE Wayland**.

Zeigt BPM, HRV (RMSSD) und ein scrollendes ECG-Signal direkt auf dem Desktop – ohne externe App, ohne Cloud, ohne Kabel.

---

## Features

- **Echtzeit-BPM** mit farbkodierter Herzfrequenz (blau → grün → orange → rot)
- **HRV (RMSSD)** aus RR-Intervallen, live aktualisiert
- **Scrollendes ECG-Signal** bei 130 Hz über den proprietären Polar PMD-Dienst
- **Automatische Verbindung** per BLE, mit Auto-Reconnect bei Verbindungsabbruch
- **Notch-Filter** (50 oder 60 Hz) gegen Netzbrumm-Artefakte, ohne QRS-Einfluss
- **R-Zacken-Detektion** (Pan-Tompkins-ähnlich) mit Markierungspunkten im Graph
- **Amplitudenskala** (±mV) am linken Rand mit automatischer Skalierung
- **Scrollgeschwindigkeit** einstellbar (6.25 / 12.5 / **25** / 50 / 100 mm/s) – per Mausrad live änderbar
- **Rahmenloses Fenster**, immer im Vordergrund, verschiebbar per Drag
- Doppelklick zum Beenden
- **Verbose-Modus** mit FPS, BLE-Sample-Rate und ECG-Rohdaten

---

## Voraussetzungen

- Linux mit BlueZ (Standard auf Arch, Ubuntu, Fedora, …)
- KDE Wayland oder X11
- Python 3.11+
- Polar H10 bereits gekoppelt (`bluetoothctl pair <MAC>`)

### Abhängigkeiten installieren

```bash
pip install bleak PyQt6
```

Auf Arch Linux (systemweites Python):

```bash
pip install bleak PyQt6 --break-system-packages
# oder in einer venv:
python -m venv ~/.venv/polar
source ~/.venv/polar/bin/activate
pip install bleak PyQt6
```

---

## Schnellstart

```bash
python polar_h10_monitor.py
```

Das Skript verbindet sich automatisch mit der fest eingetragenen MAC-Adresse des Polar H10.

---

## Optionen

```
--address ADDR          BLE MAC-Adresse des Polar H10   (Standard: 24:AC:AC:16:C6:D0)
--reconnect-delay N     Sekunden bis Reconnect-Versuch  (Standard: 3)
--opacity FLOAT         Fenster-Transparenz 0.0–1.0     (Standard: 0.92)
--font-size INT         Schriftgröße der BPM-Zahl       (Standard: 48)
--width INT             Fensterbreite in Pixeln          (Standard: 300)
--height INT            Fensterhöhe in Pixeln            (Standard: 175)
--color HEX             Akzentfarbe (ECG, Nadel, …)     (Standard: #00e676)
--bg-color HEX          Hintergrundfarbe                 (Standard: #0a0a0c)
--bg-alpha INT          Hintergrund-Alpha 0–255          (Standard: 235)
--ecg-speed FLOAT       Papiergeschwindigkeit mm/s       (Standard: 25.0)
--ecg-dpi INT           Bildschirm-DPI für mm-Umrechnung (Standard: 96)
--notch {0,50,60}       Netzbrumm-Filter Hz (0 = aus)   (Standard: 50)
--no-r-peaks            R-Zacken-Punkte ausblenden
--no-stay-on-top        Fenster nicht immer im Vordergrund
-v / --verbose          Verbose Konsolen-Output
```

### Beispiele

```bash
# Andere MAC-Adresse
python polar_h10_monitor.py --address AB:CD:EF:12:34:56

# 60 Hz Netz (USA/Japan), ohne R-Zacken-Punkte
python polar_h10_monitor.py --notch 60 --no-r-peaks

# Schnellere Papiergeschwindigkeit für QRS-Analyse
python polar_h10_monitor.py --ecg-speed 50

# Vollständig transparent, klein
python polar_h10_monitor.py --opacity 0.75 --width 220 --height 140

# Verbose (zeigt fps, BLE-Samplerate, ECG-Rohdaten)
python polar_h10_monitor.py -v
```

---

## Bedienung

| Aktion | Funktion |
|---|---|
| Linke Maustaste halten + ziehen | Fenster verschieben |
| Mausrad | ECG-Papiergeschwindigkeit ändern |
| Doppelklick | Programm beenden |

---

## ECG-Technisches

Der Polar H10 liefert ECG-Rohdaten über einen **proprietären BLE-Dienst (PMD)**:

- **Service UUID:** `fb005c80-02e7-f387-1cad-8acd2d8df0c8`
- **Control:** `fb005c81-…` – Start-Befehl wird beim Verbinden gesendet
- **Data:** `fb005c82-…` – 130 Hz Notify, Samples als 3-Byte Signed Integer (µV)

Der H10 erlaubt nur **eine einzige BLE-Verbindung** für den ECG-Stream. Falls eine andere App (Polar App, Garmin Connect, …) bereits verbunden ist, startet der ECG-Stream nicht. Die Herzfrequenz-Characteristic (`0x2A37`) funktioniert parallel.

### Notch-Filter

IIR-Filter 2. Ordnung (Bilinear-Transform), Q = 30 → ~3 Hz breite Kerbe bei 50/60 Hz. Dämpfung ~40 dB. QRS-Komplexe (0.5–40 Hz) bleiben vollständig erhalten.

### R-Zacken-Detektor

Vereinfachter Pan-Tompkins-Ansatz:
- Adaptive Schwelle (55 % des gleitenden Maximums der letzten ~1 s)
- Lokales Maximum in einem ±7-Sample-Fenster
- Refraktärperiode 34 Samples (~260 ms) gegen Doppeltrigger

### HRV (RMSSD)

Berechnet aus den letzten 20 RR-Intervallen des HR-Characteristic (`0x2A37`). Nur physiologisch plausible Intervalle (300–2000 ms) werden berücksichtigt.

---

## BPM-Farbskala

Die BPM-Zahl ändert ihre Farbe je nach Herzfrequenz:

| Bereich | Farbe | Bedeutung |
|---|---|---|
      <40 bpm  → kühles Blau    (Ruhe / Bradykardie)
      50–70    → Grün           (Normal)
      70–80   → Gelb-Orange    (leichte Belastung)
      90–110  → Orange         (mittlere Belastung)
      >110     → Rot            (hohe Belastung)

Übergänge sind weich interpoliert.

---

## Troubleshooting

**Kein ECG-Signal, aber BPM funktioniert**
Eine andere App hat den PMD-Stream exklusiv geöffnet. Polar App / Training-Apps schließen oder Polar H10 neu starten.

**`BleakError: org.bluez.Error.NotPermitted`**
BlueZ kennt das Gerät nicht als vertrauenswürdig. Im Terminal:
```bash
bluetoothctl
trust 24:AC:AC:16:C6:D0
```

**Fenster wird von anderen Fenstern überdeckt**
KDE → Rechtsklick auf Fenster → „Immer im Vordergrund" aktivieren, oder `--no-stay-on-top` weglassen.

**Schlechte Signalqualität / Artefakte**
Der H10 reagiert stark auf Bewegung und schlechten Hautkontakt. Sensor anfeuchten, Gurt straff anlegen, 4–8 Sekunden warten bis das Signal stabil ist.

---

## Lizenz

MIT
