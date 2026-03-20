# RF Stream Finder

Desktop spectrum scanner for HackRF that watches sweep output in real time, flags likely digital/data-bearing carriers, and overlays notable regions directly on the spectrum display.

## Features

- Launches `hackrf_sweep` and parses live CSV sweep rows from the official HackRF sweep format.
- Scores contiguous above-noise regions to identify likely digital streams, narrowband carriers, and other notable activity.
- Draws a live spectrum view with highlighted candidate regions and a side panel listing center frequency, bandwidth, peak level, and confidence.
- Includes a simulation mode so the GUI can be tested without a HackRF attached.

## Requirements

- Python 3.11+
- HackRF host tools installed and available on `PATH` for hardware mode
- Tkinter support in Python

## Run

```powershell
python rf_stream_finder.py
```

Use `simulation` mode to preview the app on a machine without `hackrf_sweep`. Switch to `hardware` mode when the HackRF tools are installed and the radio is connected.

## Testing

```powershell
python -m unittest -v
```

## Notes

- The detector is heuristic. It identifies spectrum regions that look like structured data activity, but it does not demodulate or decode traffic.
- HackRF sweep bin widths must stay within HackRF's supported range. The GUI validates the minimum width before starting a scan.
