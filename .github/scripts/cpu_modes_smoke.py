import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_FILE = REPO_ROOT / "clip_ai_app_v5.py"
OUT_ROOT = REPO_ROOT / "_cpu_test_output"


def _run(cmd, timeout=300):
    print(f"+ {' '.join(cmd)}", flush=True)
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    if p.stdout:
        print(p.stdout, flush=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}")


def _make_fixture_video(path: Path, duration_sec=24):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not found in PATH")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=22050",
        "-t",
        str(duration_sec),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    _run(cmd, timeout=240)


def _load_app_module():
    if not APP_FILE.exists():
        raise FileNotFoundError(f"Missing app file: {APP_FILE}")
    spec = importlib.util.spec_from_file_location("clip_app", str(APP_FILE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_scores(name, arr, expected_len):
    if arr is None:
        raise RuntimeError(f"{name}: got None")
    if len(arr) != expected_len:
        raise RuntimeError(f"{name}: len={len(arr)} expected={expected_len}")
    if not np.isfinite(np.asarray(arr)).all():
        raise RuntimeError(f"{name}: contains NaN/Inf")


def _configure_common(app, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    app.stop_flag.clear()
    app.pause_event.set()
    app._worker = appmod.VideoWorker(on_event=app._on_worker_event)

    app.out_folder.set(str(out_dir))
    app.num_clips.set(1)
    app.min_dur.set(5)
    app.max_dur.set(6)
    app.split_dur.set(5)
    app.split_limit.set(True)

    app.use_gpu.set(False)
    app.fx_backend.set("cpu")
    app.cpu_threads.set(2)
    app.gpu_vram_limit.set(30)

    # Enable all analyzers/effects to stress CPU path.
    app.opt_mirror.set(True)
    app.opt_zoom.set(True)
    app.opt_color.set(True)
    app.opt_vertical.set(True)
    app.opt_subs.set(False)
    app.speed_val.set("1x")

    app.opt_whisper.set(True)
    app.opt_laugh.set(True)
    app.opt_motion.set(True)
    app.opt_faces.set(True)

    # В cloud-тесте не нужен tkinter .after/.Text — пишем сразу в stdout.
    app._safe_log = lambda msg, tag="info": print(f"[{tag}] {msg}", flush=True)
    app._safe_progress = lambda val, text="": print(f"[progress {val:.1f}] {text}", flush=True)


def _run_mode(app, video_path: str, mode: str, timeout_sec=480):
    mode_out = OUT_ROOT / f"mode_{mode}"
    _configure_common(app, mode_out)
    app.mode.set(mode)

    t0 = time.time()
    if mode == "split":
        target = lambda: app._process_split(video_path, t0)
    elif mode == "hybrid":
        target = lambda: app._process_hybrid(video_path, t0)
    else:
        target = lambda: app._process_one(video_path, t0)

    watchdog_stop = threading.Event()

    def watchdog():
        if watchdog_stop.wait(timeout_sec):
            return
        print(f"[TIMEOUT] mode={mode} exceeded {timeout_sec}s", flush=True)
        try:
            import faulthandler
            import sys
            faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
        except Exception:
            traceback.print_exc()
        os._exit(124)

    threading.Thread(target=watchdog, daemon=True).start()

    try:
        target()  # ВАЖНО: выполняем в main-thread, иначе tkinter vars падают.
    except Exception as e:
        traceback.print_exc()
        raise RuntimeError(f"Mode '{mode}' crashed: {e}") from e
    finally:
        watchdog_stop.set()

    clips = list(mode_out.glob("*.mp4"))
    if len(clips) == 0:
        raise RuntimeError(f"Mode '{mode}' produced 0 clips")
    print(f"[OK] mode={mode} clips={len(clips)}", flush=True)


def _run_function_checks(app, video_path: str, duration: int):
    fn_out = OUT_ROOT / "functions"
    _configure_common(app, fn_out)

    tmp_audio = None
    try:
        tmp_audio = app._extract_audio(video_path)
        energy = app._calc_energy(tmp_audio, float(duration))
        _assert_scores("energy", energy, duration)

        laugh = app._analyze_laugh(energy)
        _assert_scores("laugh", laugh, duration)

        motion = app._analyze_motion_cpu(video_path, float(duration))
        _assert_scores("motion_cpu", motion, duration)

        faces = app._analyze_faces(video_path, float(duration))
        _assert_scores("faces", faces, duration)

        speech = app._analyze_speech(tmp_audio, float(duration))
        _assert_scores("speech_cpu", speech, duration)

        speech_fast = app._analyze_speech_faster(tmp_audio, float(duration), device="cpu")
        _assert_scores("speech_faster_cpu", speech_fast, duration)

        print("[OK] function checks passed", flush=True)
    finally:
        if tmp_audio and os.path.exists(tmp_audio):
            try:
                os.remove(tmp_audio)
            except Exception:
                pass


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    fixture = OUT_ROOT / "fixture.mp4"
    _make_fixture_video(fixture, duration_sec=24)

    appmod = _load_app_module()
    app = appmod.ClipAIApp()
    app.withdraw()

    try:
        modes = ["fast", "balanced", "full", "hybrid", "split"]
        for m in modes:
            _run_mode(app, str(fixture), m)

        _run_function_checks(app, str(fixture), duration=24)
        print("\nALL CPU MODE/FUNCTION TESTS PASSED", flush=True)
    finally:
        try:
            app.destroy()
        except Exception:
            pass
