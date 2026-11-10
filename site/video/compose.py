"""Turn record.py's frames and overlays into an MP4 (H.264, 30 fps), with ffmpeg.

    compose.py WORK_DIR OUT.mp4

Layers, bottom to top: the portal frames (scaled into their place on the canvas), the
chrome (background, shadow, rounded corners), the captions, the title and end cards.
Also writes OUT.jpg, a poster frame (the title card) for <video poster>. The audio track
is silence: some social sites handle a video without one badly.
"""
import json
import subprocess
import sys
from pathlib import Path

work = Path(sys.argv[1])
out = Path(sys.argv[2])
meta = json.loads((work / "timeline.json").read_text())
W, H = meta["canvas"]
X, Y, FW, FH = meta["frame"]
FPS = meta["fps"]
total = meta["total"]
FADE = 0.3

# The concat demuxer shows each frame for its duration; the last one is listed twice.
lines = []
for name, dur in meta["frames"]:
    lines += [f"file 'frames/{name}'", f"duration {dur}"]
lines.append(f"file 'frames/{meta['frames'][-1][0]}'")
(work / "frames.txt").write_text("\n".join(lines) + "\n")

inputs = ["-f", "concat", "-safe", "0", "-i", str(work / "frames.txt")]


def still(png: str, secs: float) -> int:
    """An image shown for secs; returns its input number."""
    inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{secs:.3f}", "-i", str(work / png)])
    return inputs.count("-i") - 1


def timed(png: str, start: float, end: float, fade_in: float, fade_out: float, label: str) -> str:
    """An overlay that exists only from start to end (with fades), so ffmpeg doesn't push
    a full-size image through every frame of the video."""
    dur = max(1 / FPS, end - start)
    i = still(png, dur)
    f = "format=rgba"
    if fade_in:
        f += f",fade=in:st=0:d={fade_in}:alpha=1"
    if fade_out:
        f += f",fade=out:st={max(0, dur - fade_out)}:d={fade_out}:alpha=1"
    graph.append(f"[{i}:v]{f},setpts=PTS-STARTPTS+{start}/TB[{label}]")
    return label


graph = [f"[0:v]fps={FPS},scale={FW}:{FH}:flags=lanczos,setsar=1,"
         f"pad={W}:{H}:{X}:{Y}:color=0x031b4e,format=rgba[v0]"]
i = still(meta["chrome"], total)
graph.append(f"[v0][{i}:v]overlay=0:0:format=auto[v1]")
last = "v1"
layers = [timed(c["png"], c["start"], c["end"], FADE, FADE, f"c{n}") for n, c in enumerate(meta["captions"])]
layers.append(timed(meta["title"]["png"], 0, meta["title"]["end"], 0, 0.6, "title"))
layers.append(timed(meta["end"]["png"], meta["end"]["start"], total, 0.7, 0, "end"))
for n, label in enumerate(layers):
    graph.append(f"[{last}][{label}]overlay=0:0:eof_action=pass:format=auto[l{n}]")
    last = f"l{n}"
graph.append(f"[{last}]fade=in:st=0:d=0.4,format=yuv420p[out]")

audio = inputs.count("-i")
inputs += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(graph),
       "-map", "[out]", "-map", f"{audio}:a", "-t", f"{total:.3f}", "-r", str(FPS), "-c:v", "libx264",
       "-preset", "slow", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
       "-movflags", "+faststart", str(out)]
subprocess.run(cmd, check=True)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(work / meta["title"]["png"]), "-q:v", "3",
                str(out.with_suffix(".jpg"))], check=True)
print(f"{out.name}: {total:.1f} s, {out.stat().st_size // 1024} KB")
