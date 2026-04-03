"""
clip_ai_app_v2.py — CLIP·AI Нарезчик видео v2.0 ФИНАЛ
Автор плана: пользователь + Claude
Особенности:
  - Гибрид режим: CPU + GPU параллельно
  - Усиленная защита: CPU 60%, GPU 60-70%, температурные пороги
  - Вкладки: Видео / Настройки / Эффекты / Анализ / Производительность
  - Субтитры как на шортсах, скорость, пауза, сохранение настроек
  - Умный анализ всего видео без лимита 15 минут
  - faster-whisper с корректной очисткой VRAM
"""

import os, json, shutil, threading, subprocess, platform, time, tempfile, math
import logging
from enum import Enum
from typing import Optional, List, Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import numpy as np
import wave

# Фикс чёрного экрана на Windows (DPI Awareness)
if platform.system() == "Windows":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ В ФАЙЛ
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ В ФАЙЛ (фикс #10 — разделение ошибок, трейсинг)
# ══════════════════════════════════════════════════════════════════════════════

LOG_FILE     = os.path.join(os.path.expanduser("~"), "clip_ai.log")
LOG_ERR_FILE = os.path.join(os.path.expanduser("~"), "clip_ai_errors.log")

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Общий лог — INFO и выше
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.INFO)
_fh.setFormatter(_fmt)

# Отдельный лог только для ошибок — WARNING и выше с полным трейсингом
_eh = logging.FileHandler(LOG_ERR_FILE, encoding="utf-8")
_eh.setLevel(logging.WARNING)
_eh.setFormatter(_fmt)

# Консоль — только WARNING чтобы не мусорить
_sh = logging.StreamHandler()
_sh.setLevel(logging.WARNING)
_sh.setFormatter(_fmt)

logging.basicConfig(level=logging.DEBUG, handlers=[_fh, _eh, _sh])
logger = logging.getLogger("clip_ai")
logger.info(f"Лог запущен. Файл: {LOG_FILE} | Ошибки: {LOG_ERR_FILE}")


# ══════════════════════════════════════════════════════════════════════════════
# СОСТОЯНИЕ ПРИЛОЖЕНИЯ — исключает race conditions
# ══════════════════════════════════════════════════════════════════════════════

class AppState(Enum):
    IDLE     = "idle"
    RUNNING  = "running"
    PAUSED   = "paused"
    STOPPING = "stopping"

# ══════════════════════════════════════════════════════════════════════════════
# ЦЕНТРАЛИЗОВАННАЯ ОБРАБОТКА ОШИБОК
# ══════════════════════════════════════════════════════════════════════════════

class AppError(Exception):
    """Базовый класс ошибок приложения с категориями."""
    def __init__(self, message: str, recoverable: bool = True):
        super().__init__(message)
        self.recoverable = recoverable  # True = можно продолжить, False = стоп

class FFmpegError(AppError):
    """Ошибка FFmpeg — обычно recoverable, пропускаем клип."""
    pass

class AudioError(AppError):
    """Ошибка извлечения аудио."""
    pass

class AIError(AppError):
    """Ошибка AI анализа — recoverable, пропускаем этот анализатор."""
    pass

class GPUError(AppError):
    """Ошибка GPU — recoverable, переключаемся на CPU."""
    pass

def handle_error(e: Exception, context: str, logger_inst=None, recoverable: bool = True) -> AppError:
    """
    Централизованный обработчик — логирует и оборачивает в AppError.
    Использовать везде вместо разрозненных except.
    """
    log = logger_inst or logger
    if isinstance(e, AppError):
        log.error(f"[{context}] {type(e).__name__}: {e}")
        return e
    log.exception(f"[{context}] Необработанная ошибка: {e}")
    return AppError(str(e), recoverable=recoverable)

# ══════════════════════════════════════════════════════════════════════════════
# WORKER СИСТЕМА — queue.Queue + события
# ══════════════════════════════════════════════════════════════════════════════

import queue as queue_module

class WorkerEvent:
    """Событие от worker к UI — не трогаем tkinter из потока."""
    LOG      = "log"
    PROGRESS = "progress"
    DONE     = "done"
    ERROR    = "error"

    def __init__(self, kind: str, data: dict):
        self.kind = kind
        self.data = data

class VideoWorker:
    """
    Изолированный worker для обработки видео.
    UI подписывается на события через callback — нет прямых вызовов tkinter из потока.
    Задачи приходят через queue.Queue — можно добавлять на лету.
    """

    def __init__(self, on_event: Callable[[WorkerEvent], None]):
        self._task_queue  : queue_module.Queue = queue_module.Queue()
        self._stop_flag   = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._on_event    = on_event
        self._thread      : Optional[threading.Thread] = None
        self._state_lock  = threading.Lock()

    def submit(self, fn: Callable, *args, **kwargs) -> None:
        """Добавить задачу в очередь."""
        self._task_queue.put((fn, args, kwargs))

    def start(self) -> None:
        """Запустить worker поток. Защита от двойного запуска."""
        if self._thread and self._thread.is_alive():
            logger.warning("VideoWorker уже запущен — пропускаем повторный старт")
            return
        self._stop_flag.clear()
        self._pause_event.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("VideoWorker запущен")

    def stop(self) -> None:
        """Остановить worker — дождаться завершения текущей задачи."""
        self._stop_flag.set()
        self._pause_event.set()  # разблокируем если на паузе
        self._task_queue.put(None)  # sentinel — выход из цикла
        logger.info("VideoWorker: сигнал остановки")

    def pause(self) -> None:
        self._pause_event.clear()
        logger.info("VideoWorker: пауза")

    def resume(self) -> None:
        self._pause_event.set()
        logger.info("VideoWorker: продолжение")

    def is_stopped(self) -> bool:
        return self._stop_flag.is_set()

    def wait_if_paused(self) -> bool:
        """Ждём если на паузе. Возвращает False если нужно остановиться."""
        while not self._pause_event.is_set():
            if self._stop_flag.is_set():
                return False
            time.sleep(0.1)
        return not self._stop_flag.is_set()

    def emit(self, kind: str, **data) -> None:
        """Отправить событие в UI поток."""
        self._on_event(WorkerEvent(kind, data))

    def _throttle_cpu(self) -> None:
        """
        Runtime throttle по CPU%.
        Ждём пока CPU не опустится ниже 75% — даём системе дышать.
        Вызывается перед каждой задачей и внутри тяжёлых операций.
        """
        try:
            import psutil
            while not self._stop_flag.is_set():
                cpu = psutil.cpu_percent(interval=0.5)
                if cpu < 75:
                    break
                logger.debug(f"Worker throttle: CPU {cpu:.0f}% — ждём...")
                time.sleep(0.5)
        except Exception:
            pass  # psutil недоступен — продолжаем без throttle

    def _throttle_temp(self, max_temp: int = 85) -> None:
        """
        Runtime throttle по температуре CPU.
        Ждём пока температура не упадёт ниже max_temp.
        """
        try:
            import psutil
            while not self._stop_flag.is_set():
                temps = psutil.sensors_temperatures()
                if not temps:
                    break
                entries = temps.get("coretemp") or temps.get("k10temp") or []
                if not entries:
                    break
                current = max(t.current for t in entries)
                if current <= max_temp:
                    break
                logger.debug(f"Worker temp throttle: CPU {current:.0f}°C > {max_temp}°C — ждём...")
                time.sleep(1.0)
        except Exception:
            pass  # sensors недоступны — продолжаем

    def _run(self) -> None:
        """Основной цикл worker — берёт задачи из очереди с runtime контролем."""
        logger.info("VideoWorker: цикл запущен")
        while not self._stop_flag.is_set():
            try:
                item = self._task_queue.get(timeout=1.0)
                if item is None:  # sentinel
                    break
                fn, args, kwargs = item
                try:
                    # ── Runtime контроль ПЕРЕД задачей ───────────────────────
                    self._throttle_cpu()   # ждём если CPU > 75%
                    self._throttle_temp()  # ждём если CPU temp > 85°C
                    # ── Выполняем задачу ─────────────────────────────────────
                    fn(*args, **kwargs)
                except AppError as e:
                    logger.error(f"VideoWorker задача AppError: {e}")
                    self.emit(WorkerEvent.ERROR, message=str(e), recoverable=e.recoverable)
                except Exception as e:
                    err = handle_error(e, "VideoWorker._run")
                    self.emit(WorkerEvent.ERROR, message=str(err), recoverable=True)
                finally:
                    # Гарантированный cleanup после каждой задачи
                    try:
                        import gc; gc.collect()
                        try:
                            import torch
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                        except Exception:
                            pass
                    except Exception:
                        pass
                    self._task_queue.task_done()
            except queue_module.Empty:
                continue
        logger.info("VideoWorker: цикл завершён")
        self.emit(WorkerEvent.DONE, message="Очередь завершена")

# ══════════════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ══════════════════════════════════════════════════════════════════════════════

class Config:
    """Все настройки в одном месте — никаких магических чисел по коду."""

    # Файлы
    SETTINGS_FILE   : str = os.path.join(os.path.expanduser("~"), ".clip_ai_v2_settings.json")

    # Нарезка
    MIN_GAP         : int = 10    # минимальный зазор между клипами (сек)

    # Безопасные дефолты — программа НЕ перегружает железо
    CPU_THREAD_PCT  : int = 60    # % от всех ядер
    GPU_VRAM_PCT    : int = 60    # % VRAM
    CPU_TEMP_MAX    : int = 80    # °C
    GPU_TEMP_MAX    : int = 75    # °C
    CPU_RESERVED_CORES : int = 2  # всегда оставляем системе минимум 2 ядра
    CPU_MAX_PCT_CAP    : int = 75 # верхний cap потоков от общего числа ядер
    GPU_VRAM_MIN_PCT   : int = 30
    GPU_VRAM_MAX_PCT   : int = 90
    CPU_TEMP_MIN_C     : int = 60
    CPU_TEMP_MAX_C     : int = 90
    GPU_TEMP_MIN_C     : int = 60
    GPU_TEMP_MAX_C     : int = 85

    # Мониторинг и защита
    TEMP_CHECK_SEC  : int   = 2    # интервал проверки температуры
    TEMP_COOLDOWN   : int   = 8    # °C ниже порога = продолжаем
    TEMP_TIMEOUT    : int   = 300  # максимум секунд ожидания охлаждения
    THROTTLE_SLEEP  : float = 0.3  # пауза между тяжёлыми операциями

    # FFmpeg
    FFMPEG_TIMEOUT  : int = 3600   # таймаут на весь процесс
    CLIP_TIMEOUT    : int = 600    # таймаут на один клип (10 минут)
    PROBE_TIMEOUT   : int = 30     # таймаут ffprobe

    # Анализ
    MOTION_SKIP_BASE : int = 12    # базовый skip кадров для движения
    FACES_SKIP_BASE  : int = 15    # базовый skip кадров для лиц
    MOTION_ADAPT_DIV : int = 300   # делитель для адаптивного skip
    FACES_ADAPT_DIV  : int = 240   # делитель для адаптивного skip
    BALANCED_MOTION_SKIP_MULT : float = 3.0  # balanced анализ движения заметно быстрее
    FACE_GPU_MODELS : tuple = (
        "face_detection_yunet_2023mar.onnx",
        "face_detection_yunet_2022mar.onnx",
        "face_detector_yunet.onnx",
        "yunet.onnx",
    )
    FACE_GPU_INPUT_SIZE : int = 320
    FACE_GPU_SCORE_THRESHOLD : float = 0.75
    FACE_GPU_NMS_THRESHOLD : float = 0.30
    FACE_GPU_TOP_K : int = 2000
    FACE_TASK_MODELS : tuple = (
        "face_detection_short_range.tflite",
        "face_detector.tflite",
        "blaze_face_short_range.tflite",
    )
    AUDIO_MAX_SAMPLES: int = 50_000_000  # макс сэмплов аудио
    LAUGH_THRESHOLD  : float = 2.5  # множитель среднего для детекта смеха
    WHISPER_VRAM_MB  : int = 500    # примерный VRAM для Whisper tiny
    FACES_VRAM_MB    : int = 300    # примерный VRAM для MediaPipe

# Алиасы для совместимости
MIN_GAP              = Config.MIN_GAP
SETTINGS_FILE        = Config.SETTINGS_FILE
DEFAULT_CPU_THREAD_PCT = Config.CPU_THREAD_PCT
DEFAULT_GPU_VRAM_PCT = Config.GPU_VRAM_PCT
DEFAULT_CPU_TEMP_MAX = Config.CPU_TEMP_MAX
DEFAULT_GPU_TEMP_MAX = Config.GPU_TEMP_MAX
TEMP_CHECK_INTERVAL  = Config.TEMP_CHECK_SEC
TEMP_COOLDOWN_DELTA  = Config.TEMP_COOLDOWN
THROTTLE_SLEEP       = Config.THROTTLE_SLEEP

# ══════════════════════════════════════════════════════════════════════════════
# RETRY УТИЛИТА
# ══════════════════════════════════════════════════════════════════════════════

def with_retry(fn: Callable, retries: int = 2, delay: float = 2.0,
               error_cls: type = AppError, context: str = "") -> any:
    """
    Выполняет fn с повторными попытками при ошибке.
    retries — сколько раз повторить после первой неудачи.
    delay   — пауза между попытками в секундах.
    Бросает последнее исключение если все попытки провалились.
    """
    last_exc = None
    for attempt in range(1, retries + 2):  # +2 = первая попытка + retries
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt <= retries:
                logger.warning(f"[{context}] Попытка {attempt} не удалась: {e} — повтор через {delay}с")
                time.sleep(delay)
            else:
                logger.error(f"[{context}] Все {retries + 1} попыток провалились: {e}")
    raise error_cls(str(last_exc), recoverable=True)

# ══════════════════════════════════════════════════════════════════════════════
# ЦВЕТА И ШРИФТЫ
# ══════════════════════════════════════════════════════════════════════════════

# Палитра по запросу: чёрный + красный + зелёный + фиолетовый.
BG       = "#050505"
BG2      = "#0E0E10"
BG3      = "#17171D"
BG4      = "#23232B"
RED      = "#FF3B3B"
RED2     = "#FF5959"
PURPLE   = "#8A2BE2"
PURPLE2  = "#B266FF"
PINK     = "#A855F7"
TEXT     = "#F2F2F5"
TEXT_DIM = "#A9A9B4"
TEXT_MED = "#D8D8E3"
SUCCESS  = "#39D353"
DANGER   = "#FF4D4F"
WARN     = "#B266FF"
BORDER   = "#3A2362"

FONT_LABEL = ("Bahnschrift SemiBold", 10)
FONT_MONO  = ("Consolas", 9)
FONT_BTN   = ("Bahnschrift SemiBold", 10)
FONT_SMALL = ("Segoe UI", 9)

NO_WINDOW = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0

# ══════════════════════════════════════════════════════════════════════════════
# СИСТЕМНЫЕ УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════════════════

def get_cpu_count() -> int:
    try: return os.cpu_count() or 4
    except Exception: return 4

def safe_cpu_threads(pct: int) -> int:
    """Возвращает количество потоков CPU — минимум 1, оставляем хотя бы 1 ядро системе."""
    total = get_cpu_count()
    if total <= 1:
        return 1
    count = max(1, min(total - 1, math.ceil(total * pct / 100)))
    return count

def apply_cpu_limit(threads: int) -> None:
    """Применяет ограничение потоков CPU через переменные окружения."""
    t = str(threads)
    for var in ["OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"]:
        os.environ[var] = t

def set_process_priority_low() -> None:
    """Понижаем приоритет процесса — система работает плавнее."""
    try:
        import psutil
        p = psutil.Process()
        if platform.system() == "Windows":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
        logger.info("Приоритет процесса снижен")
    except Exception as e:
        logger.debug(f"Не удалось снизить приоритет процесса: {e}")

def apply_cpu_affinity(threads: int) -> None:
    """
    Ограничиваем процесс конкретным числом ядер через cpu_affinity.
    Это реально работает — в отличие от OMP_NUM_THREADS который FFmpeg игнорирует.
    """
    try:
        import psutil
        p      = psutil.Process()
        total  = get_cpu_count()
        count  = max(1, min(threads, total))
        # Берём первые N ядер
        cores  = list(range(total))[:count]
        p.cpu_affinity(cores)
        logger.info(f"CPU affinity: {count} из {total} ядер ({cores})")
    except AttributeError:
        # cpu_affinity не поддерживается на этой платформе
        logger.debug("cpu_affinity недоступен — используем только OMP_NUM_THREADS")
    except Exception as e:
        logger.debug(f"cpu_affinity ошибка: {e}")

def open_folder(path: str) -> None:
    if platform.system() == "Windows": os.startfile(path)
    elif platform.system() == "Darwin": subprocess.run(["open", path])
    else: subprocess.run(["xdg-open", path])

# ══════════════════════════════════════════════════════════════════════════════
# GPU МЕНЕДЖЕР — вся работа с GPU через один класс
# ══════════════════════════════════════════════════════════════════════════════

class GPUManager:
    """
    Централизованное управление GPU.
    Все операции безопасны — никогда не падают, всегда есть fallback на CPU.
    """

    def __init__(self):
        self.available    : bool = False
        self.name         : str  = "Недоступен"
        self.vram_total   : int  = 0
        self.cuda_version : str  = ""
        self._nvml_ok     : bool = False
        self._detect()

    def _detect(self) -> None:
        """Определяем GPU при старте. Никогда не бросает исключение."""
        try:
            import torch
            if torch.cuda.is_available():
                self.available    = True
                self.name         = torch.cuda.get_device_name(0)
                props             = torch.cuda.get_device_properties(0)
                self.vram_total   = props.total_memory // (1024 * 1024)
                self.cuda_version = torch.version.cuda or ""
                logger.info(f"GPU обнаружен: {self.name}, VRAM {self.vram_total}MB, CUDA {self.cuda_version}")
        except Exception as e:
            logger.debug(f"GPU (torch) недоступен: {e}")

        try:
            import ctypes
            # Загружаем nvml.dll напрямую из System32 (фикс для Windows)
            ctypes.CDLL("C:\\Windows\\System32\\nvml.dll")
            import pynvml
            pynvml.nvmlInit()
            self._nvml_ok = True
            logger.debug("pynvml инициализирован успешно")
        except Exception as e:
            self._nvml_ok = False
            logger.debug(f"pynvml недоступен: {e}")

    def temperature(self) -> Optional[int]:
        """Температура GPU в °C. Пробует pynvml, потом nvidia-smi."""
        # Попытка через pynvml
        if self._nvml_ok:
            try:
                import pynvml
                h = pynvml.nvmlDeviceGetHandleByIndex(0)
                return pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception as e:
                logger.debug(f"pynvml temperature fallback: {e}")

        # Fallback — nvidia-smi напрямую
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW)
            if res.returncode == 0:
                val = res.stdout.strip()
                if val.isdigit():
                    return int(val)
        except Exception as e:
            logger.debug(f"nvidia-smi temperature ошибка: {e}")
        return None

    def utilization_pct(self) -> Optional[int]:
        """Загрузка GPU в процентах. Пробует pynvml, потом nvidia-smi."""
        if self._nvml_ok:
            try:
                import pynvml
                h = pynvml.nvmlDeviceGetHandleByIndex(0)
                util = pynvml.nvmlDeviceGetUtilizationRates(h)
                val = int(getattr(util, "gpu", 0))
                return clamp(val, 0, 100)
            except Exception as e:
                logger.debug(f"pynvml utilization fallback: {e}")

        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, creationflags=NO_WINDOW
            )
            if res.returncode == 0:
                raw = (res.stdout or "").strip().splitlines()
                if raw:
                    txt = raw[0].strip().replace("%", "")
                    if txt.isdigit():
                        return clamp(int(txt), 0, 100)
        except Exception as e:
            logger.debug(f"nvidia-smi utilization ошибка: {e}")
        return None

    def vram_used_mb(self) -> Optional[int]:
        """Занятый VRAM в MB. Сначала pynvml, потом torch как fallback."""
        # Попытка через pynvml
        if self._nvml_ok:
            try:
                import pynvml
                h   = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                return mem.used // (1024 * 1024)
            except Exception as e:
                logger.debug(f"pynvml vram_used fallback to torch: {e}")

        # Fallback — torch (всегда доступен если GPU есть)
        try:
            import torch
            if torch.cuda.is_available():
                return torch.cuda.memory_allocated(0) // (1024 * 1024)
        except Exception as e:
            logger.debug(f"torch vram_used ошибка: {e}")
        return None

    def vram_free_mb(self) -> Optional[int]:
        """Свободный VRAM в MB. Сначала pynvml, потом torch как fallback."""
        # Попытка через pynvml
        if self._nvml_ok:
            try:
                import pynvml
                h   = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                return mem.free // (1024 * 1024)
            except Exception as e:
                logger.debug(f"pynvml vram_free fallback to torch: {e}")

        # Fallback — torch
        try:
            import torch
            if torch.cuda.is_available():
                total = torch.cuda.get_device_properties(0).total_memory
                used  = torch.cuda.memory_allocated(0)
                return (total - used) // (1024 * 1024)
        except Exception as e:
            logger.debug(f"torch vram_free ошибка: {e}")

        # Последний fallback — если совсем ничего нет, считаем что 50% свободно
        if self.vram_total > 0:
            logger.warning("VRAM данные недоступны — используем 50% как оценку")
            return self.vram_total // 2
        return None

    def vram_pct_used(self) -> Optional[int]:
        """Процент занятого VRAM. None если недоступно."""
        used = self.vram_used_mb()
        if used is None or self.vram_total == 0: return None
        return int(used / self.vram_total * 100)

    def can_fit_model(self, model_mb: int, limit_pct: int) -> bool:
        """
        Проверяет хватает ли VRAM для загрузки модели с учётом лимита.
        Если данные о VRAM недоступны — разрешаем GPU (не блокируем зря).
        """
        if not self.available or self.vram_total == 0: return False
        free = self.vram_free_mb()
        if free is None:
            # Данных нет — не блокируем GPU, лучше попробовать и поймать ошибку
            logger.warning(f"can_fit_model: VRAM данные недоступны → разрешаем GPU для {model_mb}MB")
            return True
        allowed_mb = int(self.vram_total * limit_pct / 100)
        used       = self.vram_used_mb() or 0
        result     = (used + model_mb) <= allowed_mb
        logger.debug(f"can_fit_model({model_mb}MB): used={used}MB, allowed={allowed_mb}MB → {result}")
        return result

    def clear_vram(self) -> None:
        """Принудительная очистка VRAM кэша PyTorch."""
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
                logger.debug("VRAM очищен")
        except Exception as e:
            logger.debug(f"Ошибка очистки VRAM: {e}")

    def short_name(self) -> str:
        return self.name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")


# Глобальный экземпляр GPU менеджера
GPU = GPUManager()

# ══════════════════════════════════════════════════════════════════════════════
# FFMPEG ПОИСК
# ══════════════════════════════════════════════════════════════════════════════

def find_ff(name):
    ff = shutil.which(name)
    if ff: return ff
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.exe")
    if os.path.exists(local): return local
    for c in [
        rf"C:\ffmpeg\bin\{name}.exe",
        rf"C:\Program Files\ffmpeg\bin\{name}.exe",
        os.path.expanduser(rf"~\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin\{name}.exe"),
    ]:
        if os.path.exists(c): return c
    return None

FFMPEG  = find_ff("ffmpeg")
FFPROBE = find_ff("ffprobe")

if not FFMPEG:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("FFmpeg не найден", "FFmpeg не найден!\n\nwinget install ffmpeg")
    _r.destroy()
    raise SystemExit(1)

# ══════════════════════════════════════════════════════════════════════════════
# МАТЕМАТИКА
# ══════════════════════════════════════════════════════════════════════════════

def normalize(arr):
    arr = np.array(arr, dtype=np.float32)
    if len(arr) == 0: return arr
    arr = np.nan_to_num(arr, nan=0.0)
    mx  = np.nanmax(arr)
    return arr / mx if mx > 0 else arr

def smooth(arr, k=5):
    if len(arr) < k: return arr
    return np.convolve(arr, np.ones(k) / k, mode='same')

def safe_add(score, new_score, weight):
    if new_score is None or len(new_score) == 0 or np.max(new_score) == 0: return score
    return score + normalize(new_score) * weight

def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))

# ══════════════════════════════════════════════════════════════════════════════
# UI ВИДЖЕТЫ
# ══════════════════════════════════════════════════════════════════════════════

class NumStepper(tk.Frame):
    def __init__(self, parent, var, from_=1, to=30, width=4, **kw):
        bg = parent.cget("bg")
        super().__init__(parent, bg=bg, **kw)
        self.var = var; self.from_ = from_; self.to = to
        bk = dict(bg=BG4, fg=TEXT_MED, font=("Bahnschrift SemiBold", 11), bd=0,
                  relief="flat", cursor="hand2", width=2,
                  activebackground=PURPLE, activeforeground=TEXT)
        tk.Button(self, text="−", command=self._dec, **bk).pack(side="left")
        tk.Label(self, textvariable=var, font=("Bahnschrift SemiBold", 11),
                 bg=bg, fg=TEXT, width=width).pack(side="left", padx=4)
        tk.Button(self, text="+", command=self._inc, **bk).pack(side="left")

    def _dec(self):
        if self.var.get() > self.from_: self.var.set(self.var.get() - 1)

    def _inc(self):
        if self.var.get() < self.to: self.var.set(self.var.get() + 1)


class Toggle(tk.Frame):
    def __init__(self, parent, var, label="", **kw):
        bg = parent.cget("bg")
        super().__init__(parent, bg=bg, **kw)
        self.var = var
        self.cv  = tk.Canvas(self, width=36, height=18, bg=bg,
                             highlightthickness=0, cursor="hand2")
        self.cv.pack(side="left", padx=(0, 6))
        if label:
            tk.Label(self, text=label, font=FONT_SMALL, bg=bg, fg=TEXT_MED).pack(side="left")
        self.cv.bind("<Button-1>", self._toggle)
        self._draw()

    def _toggle(self, e=None):
        self.var.set(not self.var.get()); self._draw()

    def _draw(self):
        self.cv.delete("all")
        on = self.var.get(); c = PURPLE if on else BG4
        self.cv.create_oval(0, 1, 18, 17, fill=c, outline="")
        self.cv.create_oval(18, 1, 36, 17, fill=c, outline="")
        self.cv.create_rectangle(9, 1, 27, 17, fill=c, outline="")
        x = 20 if on else 8
        self.cv.create_oval(x - 7, 2, x + 7, 16, fill=TEXT, outline="")


class TabBar(tk.Frame):
    def __init__(self, parent, tabs, on_change, **kw):
        super().__init__(parent, bg=BG2, **kw)
        self.buttons  = {}
        self._order   = [k for k, _ in tabs]
        self._visible_keys = set(self._order)
        self.active   = tk.StringVar(value=tabs[0][0])
        self.on_change = on_change
        for key, label in tabs:
            btn = tk.Button(self, text=label, font=FONT_BTN, bd=0, padx=16, pady=9,
                            relief="flat", cursor="hand2",
                            highlightthickness=1, highlightbackground=BORDER, highlightcolor=PURPLE2,
                            command=lambda k=key: self._select(k))
            btn.pack(side="left", padx=(0, 6), pady=4)
            self.buttons[key] = btn
        self._update()

    def _select(self, key):
        if key not in self._visible_keys:
            return
        self.active.set(key); self._update(); self.on_change(key)

    def set_visible(self, keys):
        visible = {k for k in keys if k in self.buttons}
        if not visible:
            visible = {self._order[0]}
        self._visible_keys = visible
        for key in self._order:
            btn = self.buttons.get(key)
            if not btn:
                continue
            btn.pack_forget()
            if key in self._visible_keys:
                btn.pack(side="left")
        if self.active.get() not in self._visible_keys:
            for key in self._order:
                if key in self._visible_keys:
                    self.active.set(key)
                    break
        self._update()

    def _update(self):
        for key, btn in self.buttons.items():
            active = self.active.get() == key
            btn.configure(
                bg=PURPLE if active else BG2,
                fg=BG if active else TEXT_DIM,
                activebackground=PURPLE2 if active else BG3,
                activeforeground=BG if active else TEXT_MED
            )

# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ ПРИЛОЖЕНИЕ
# ══════════════════════════════════════════════════════════════════════════════

class ClipAIApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("CLIP·AI v2.0 — Нарезчик видео")
        self.geometry("980x900")
        self.minsize(820, 720)
        self.configure(bg=BG)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        # ── Состояние (AppState enum исключает race conditions) ───────────────
        self.video_paths   : List[str] = []
        self._state        = AppState.IDLE
        self._state_lock   = threading.Lock()
        self.pause_event   = threading.Event()
        self.pause_event.set()
        self.stop_flag     = threading.Event()
        self.whisper_model = None
        self._last_temp_check = 0
        self._warned_no_cpu_temp_sensor = False
        self._warned_no_gpu_temp_sensor = False
        self._warned_no_face_task_model = False
        self._warned_no_face_gpu_model = False
        self._warned_no_face_gpu_runtime = False
        self._warned_motion_gpu_runtime = False
        self._motion_gpu_cap_checked = False
        self._motion_gpu_capable = False
        self._cpu_temp_fallback_cache: Optional[float] = None
        self._cpu_temp_fallback_ts: float = 0.0

        # ── Worker система ────────────────────────────────────────────────────
        self._worker = VideoWorker(on_event=self._on_worker_event)

        # ── Инициализация переменных ─────────────────────────────────────────
        self._init_vars()

        # ── Загрузка настроек и старт ────────────────────────────────────────
        self._load_settings()
        self._apply_cpu_limit()
        set_process_priority_low()
        self.withdraw()
        self._build_ui()
        self._monitor_loop()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.update_idletasks()
        self.deiconify()
        self.lift()
        self.focus_force()

    @property
    def running(self) -> bool:
        return self._state in (AppState.RUNNING, AppState.PAUSED)

    @property
    def paused(self) -> bool:
        return self._state == AppState.PAUSED

    def _set_state(self, state: AppState) -> None:
        with self._state_lock:
            logger.info(f"Состояние: {self._state.value} → {state.value}")
            self._state = state

    def _on_worker_event(self, event: WorkerEvent) -> None:
        """Получаем события от worker и передаём в UI через after() — безопасно."""
        self.after(0, lambda: self._handle_worker_event(event))

    def _handle_worker_event(self, event: WorkerEvent) -> None:
        """Обрабатываем события worker в UI потоке."""
        if event.kind == WorkerEvent.ERROR:
            msg = event.data.get("message", "Неизвестная ошибка")
            recoverable = event.data.get("recoverable", True)
            self._log(f"{'Ошибка (продолжаем)' if recoverable else 'Критическая ошибка'}: {msg}", "error")
            logger.error(f"Worker event ERROR: {msg} (recoverable={recoverable})")
        elif event.kind == WorkerEvent.DONE:
            self._set_state(AppState.IDLE)
            self.btn_run.configure(state="normal", bg=RED, text="ЗАПУСТИТЬ НАРЕЗКУ")
            self.btn_pause.configure(state="disabled")
            logger.info("Worker завершил все задачи")

        # ── Переменные — Видео ───────────────────────────────────────────────
    def _init_vars(self):
        """Инициализация всех tkinter переменных."""
        self.yt_url     = tk.StringVar(value="")
        self.out_folder = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Desktop", "CLIPS"))

        # ── Переменные — Настройки ───────────────────────────────────────────
        self.num_clips  = tk.IntVar(value=10)
        self.min_dur    = tk.IntVar(value=30)
        self.max_dur    = tk.IntVar(value=60)
        self.mode       = tk.StringVar(value="balanced")
        self.split_dur  = tk.IntVar(value=60)
        self.split_limit = tk.BooleanVar(value=False)

        # ── Переменные — Эффекты ─────────────────────────────────────────────
        self.opt_mirror   = tk.BooleanVar(value=False)
        self.opt_zoom     = tk.BooleanVar(value=False)
        self.opt_color    = tk.BooleanVar(value=False)
        self.opt_vertical = tk.BooleanVar(value=False)
        self.opt_subs     = tk.BooleanVar(value=False)
        self.speed_val    = tk.StringVar(value="1x")
        self.fx_backend   = tk.StringVar(value="gpu" if GPU.available else "cpu")

        # ── Переменные — AI анализ ───────────────────────────────────────────
        self.opt_whisper = tk.BooleanVar(value=True)
        self.opt_laugh   = tk.BooleanVar(value=True)
        self.opt_motion  = tk.BooleanVar(value=True)
        self.opt_faces   = tk.BooleanVar(value=True)
        self.w_speech    = tk.IntVar(value=3)
        self.w_laugh     = tk.IntVar(value=2)
        self.w_motion    = tk.IntVar(value=2)
        self.w_faces     = tk.IntVar(value=2)
        self.w_audio     = tk.IntVar(value=1)
        self.keywords    = tk.StringVar(value="")
        self.ui_expert_mode = tk.BooleanVar(value=False)

        # ── Переменные — Производительность (БЕЗОПАСНЫЕ ДЕФОЛТЫ) ─────────────
        default_threads       = safe_cpu_threads(DEFAULT_CPU_THREAD_PCT)
        self.cpu_threads      = tk.IntVar(value=default_threads)
        self.gpu_vram_limit   = tk.IntVar(value=DEFAULT_GPU_VRAM_PCT)
        self.temp_cpu_max     = tk.IntVar(value=DEFAULT_CPU_TEMP_MAX)
        self.temp_gpu_max     = tk.IntVar(value=DEFAULT_GPU_TEMP_MAX)
        self.use_gpu          = tk.BooleanVar(value=GPU.available)

        # ── Прогресс ─────────────────────────────────────────────────────────
        self.progress = tk.DoubleVar(value=0)

    # ══════════════════════════════════════════════════════════════════════════
    # НАСТРОЙКИ
    # ══════════════════════════════════════════════════════════════════════════

    def _settings_map(self):
        return {
            "num_clips": self.num_clips, "min_dur": self.min_dur,
            "max_dur": self.max_dur, "mode": self.mode,
            "split_dur": self.split_dur, "split_limit": self.split_limit,
            "out_folder": self.out_folder, "keywords": self.keywords,
            "opt_mirror": self.opt_mirror, "opt_zoom": self.opt_zoom,
            "opt_color": self.opt_color, "opt_vertical": self.opt_vertical,
            "opt_subs": self.opt_subs, "speed_val": self.speed_val,
            "fx_backend": self.fx_backend,
            "opt_whisper": self.opt_whisper, "opt_laugh": self.opt_laugh,
            "opt_motion": self.opt_motion, "opt_faces": self.opt_faces,
            "w_speech": self.w_speech, "w_laugh": self.w_laugh,
            "w_motion": self.w_motion, "w_faces": self.w_faces,
            "w_audio": self.w_audio, "cpu_threads": self.cpu_threads,
            "gpu_vram_limit": self.gpu_vram_limit,
            "temp_cpu_max": self.temp_cpu_max, "temp_gpu_max": self.temp_gpu_max,
            "use_gpu": self.use_gpu,
            "ui_expert_mode": self.ui_expert_mode,
        }

    def _load_settings(self) -> None:
        try:
            if not os.path.exists(SETTINGS_FILE): return
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            loaded = 0
            for attr, var in self._settings_map().items():
                if attr in s:
                    try:
                        var.set(s[attr])
                        loaded += 1
                    except Exception as e:
                        logger.debug(f"Не удалось загрузить настройку {attr}: {e}")
            self._sanitize_safety_settings(source="load_settings")
            logger.info(f"Настройки загружены: {loaded} параметров")
        except Exception as e:
            logger.warning(f"Не удалось загрузить настройки: {e}")

    def _save_settings(self) -> None:
        try:
            s = {attr: var.get() for attr, var in self._settings_map().items()}
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
            logger.info("Настройки сохранены")
        except Exception as e:
            logger.warning(f"Не удалось сохранить настройки: {e}")

    def _max_safe_cpu_threads(self) -> int:
        total = get_cpu_count()
        cap_by_pct = max(1, math.ceil(total * Config.CPU_MAX_PCT_CAP / 100))
        cap_by_reserve = max(1, total - Config.CPU_RESERVED_CORES)
        return max(1, min(total, cap_by_pct, cap_by_reserve))

    def _sanitize_safety_settings(self, source: str = "runtime") -> None:
        def _to_int(value, fallback: int) -> int:
            try:
                return int(value)
            except Exception:
                return fallback

        def _to_bool(value, fallback: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "yes", "on"):
                    return True
                if v in ("0", "false", "no", "off"):
                    return False
                return fallback
            try:
                return bool(value)
            except Exception:
                return fallback

        def _to_backend(value, fallback: str) -> str:
            try:
                v = str(value).strip().lower()
            except Exception:
                return fallback
            return v if v in ("cpu", "gpu") else fallback

        safe_cpu = clamp(_to_int(self.cpu_threads.get(), safe_cpu_threads(DEFAULT_CPU_THREAD_PCT)), 1, self._max_safe_cpu_threads())
        safe_vram = clamp(_to_int(self.gpu_vram_limit.get(), DEFAULT_GPU_VRAM_PCT), Config.GPU_VRAM_MIN_PCT, Config.GPU_VRAM_MAX_PCT)
        safe_cpu_temp = clamp(_to_int(self.temp_cpu_max.get(), DEFAULT_CPU_TEMP_MAX), Config.CPU_TEMP_MIN_C, Config.CPU_TEMP_MAX_C)
        safe_gpu_temp = clamp(_to_int(self.temp_gpu_max.get(), DEFAULT_GPU_TEMP_MAX), Config.GPU_TEMP_MIN_C, Config.GPU_TEMP_MAX_C)
        safe_use_gpu = _to_bool(self.use_gpu.get(), GPU.available) and GPU.available
        safe_fx_backend = _to_backend(self.fx_backend.get(), "gpu" if GPU.available else "cpu")
        if safe_fx_backend == "gpu" and not safe_use_gpu:
            safe_fx_backend = "cpu"

        changed = (
            safe_cpu != self.cpu_threads.get() or
            safe_vram != self.gpu_vram_limit.get() or
            safe_cpu_temp != self.temp_cpu_max.get() or
            safe_gpu_temp != self.temp_gpu_max.get() or
            safe_use_gpu != bool(self.use_gpu.get()) or
            safe_fx_backend != str(self.fx_backend.get()).strip().lower()
        )

        self.cpu_threads.set(safe_cpu)
        self.gpu_vram_limit.set(safe_vram)
        self.temp_cpu_max.set(safe_cpu_temp)
        self.temp_gpu_max.set(safe_gpu_temp)
        self.use_gpu.set(safe_use_gpu)
        self.fx_backend.set(safe_fx_backend)

        if changed:
            logger.warning(
                f"Безопасные лимиты применены ({source}): "
                f"CPU={safe_cpu}, VRAM={safe_vram}%, CPU_temp={safe_cpu_temp}°C, "
                f"GPU_temp={safe_gpu_temp}°C, use_gpu={safe_use_gpu}, fx_backend={safe_fx_backend}"
            )

    def _apply_cpu_limit(self) -> None:
        """Применяет ограничение CPU — и через env vars и через affinity."""
        self._sanitize_safety_settings(source="apply_cpu_limit")
        threads = self.cpu_threads.get()
        apply_cpu_limit(threads)       # OMP_NUM_THREADS и т.д. — для numpy/torch
        apply_cpu_affinity(threads)    # реальное ограничение ядер — для FFmpeg тоже

    def _on_close(self) -> None:
        """Graceful shutdown — сначала останавливаем worker и потоки, потом закрываем."""
        logger.info("Закрытие приложения...")
        self._set_state(AppState.STOPPING)
        self.stop_flag.set()
        self.pause_event.set()
        self._worker.stop()
        self._save_settings()
        self._cleanup_whisper()
        GPU.clear_vram()
        logger.info("Приложение закрыто")
        self.destroy()

    def _cleanup_whisper(self) -> None:
        """Корректно освобождаем память после Whisper."""
        try:
            if self.whisper_model is not None:
                logger.info("Выгружаю Whisper модель...")
                del self.whisper_model
                self.whisper_model = None
                GPU.clear_vram()
                logger.info("Whisper выгружен, VRAM очищен")
        except Exception as e:
            logger.warning(f"Ошибка при выгрузке Whisper: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # МОНИТОРИНГ ТЕМПЕРАТУРЫ И VRAM
    # ══════════════════════════════════════════════════════════════════════════

    def _monitor_loop(self):
        """Обновляем индикаторы каждые 2 секунды."""
        try:
            temp = GPU.temperature()
            vram_used = GPU.vram_used_mb()
            vram_pct  = GPU.vram_pct_used()

            if temp is not None:
                limit = self.temp_gpu_max.get()
                if temp >= limit:
                    color = DANGER
                elif temp >= limit - 10:
                    color = WARN
                else:
                    color = SUCCESS
                self.lbl_gpu_temp.configure(text=f"GPU {temp}°C", fg=color)
            else:
                self.lbl_gpu_temp.configure(text="GPU --°C", fg=TEXT_DIM)

            if vram_used is not None and GPU.vram_total > 0:
                vram_limit = self.gpu_vram_limit.get()
                vram_color = DANGER if (vram_pct or 0) >= vram_limit else TEXT_MED
                self.lbl_vram.configure(
                    text=f"VRAM {vram_used}MB / {GPU.vram_total}MB ({vram_pct}%)",
                    fg=vram_color)
            else:
                self.lbl_vram.configure(text="VRAM --", fg=TEXT_DIM)

            cpu_t = self._read_cpu_temp()
            if hasattr(self, 'lbl_cpu_temp'):
                if cpu_t is not None:
                    cpu_limit = self.temp_cpu_max.get()
                    cpu_color = DANGER if cpu_t >= cpu_limit else (WARN if cpu_t >= cpu_limit - 10 else SUCCESS)
                    self.lbl_cpu_temp.configure(text=f"CPU {cpu_t:.0f}°C", fg=cpu_color)
                else:
                    self.lbl_cpu_temp.configure(text="CPU --°C", fg=TEXT_DIM)
        except Exception as e:
            logger.debug(f"_monitor_loop error: {e}")
        self.after(TEMP_CHECK_INTERVAL * 1000, self._monitor_loop)

    def _read_cpu_temp(self) -> Optional[float]:
        """Пытается получить температуру CPU через psutil с fallback по разным сенсорам."""
        try:
            import psutil
            temps = psutil.sensors_temperatures() or {}
        except Exception as e:
            logger.debug(f"_read_cpu_temp: psutil sensor error: {e}")
            return self._read_cpu_temp_windows_acpi()

        if not temps:
            return self._read_cpu_temp_windows_acpi()

        # Приоритетные датчики CPU на популярных платформах
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "cpu-thermal", "zenpower"):
            entries = temps.get(key) or []
            vals = [getattr(t, "current", None) for t in entries]
            vals = [v for v in vals if v is not None]
            if vals:
                return float(max(vals))

        # Fallback: ищем CPU-похожие метки/чипы
        candidates = []
        for chip, entries in temps.items():
            chip_name = (chip or "").lower()
            for t in entries:
                cur = getattr(t, "current", None)
                if cur is None:
                    continue
                label = (getattr(t, "label", "") or "").lower()
                if any(k in label for k in ("cpu", "package", "tctl", "core", "ccd")) or \
                   any(k in chip_name for k in ("cpu", "coretemp", "k10temp", "zenpower", "acpi")):
                    candidates.append(float(cur))
        if candidates:
            return max(candidates)

        return self._read_cpu_temp_windows_acpi()

    def _read_cpu_temp_windows_acpi(self) -> Optional[float]:
        """
        Fallback для Windows: пробуем ACPI-датчик через WMI (без внешних библиотек).
        Важно: на части ноутбуков этот источник пустой или неточный.
        """
        if platform.system() != "Windows":
            return None

        now = time.time()
        if (now - self._cpu_temp_fallback_ts) < 5.0:
            return self._cpu_temp_fallback_cache

        self._cpu_temp_fallback_ts = now
        self._cpu_temp_fallback_cache = None

        ps_cmd = (
            "(Get-CimInstance -Namespace root/wmi -ClassName "
            "MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty CurrentTemperature) -join ','"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=2, creationflags=NO_WINDOW
            )
            if res.returncode != 0:
                return None

            raw = (res.stdout or "").strip()
            if not raw:
                return None

            vals = []
            for token in raw.replace("\r", "\n").replace(",", "\n").split():
                try:
                    celsius = (float(token) / 10.0) - 273.15
                except Exception:
                    continue
                if 1.0 <= celsius <= 130.0:
                    vals.append(celsius)

            if vals:
                self._cpu_temp_fallback_cache = max(vals)
        except Exception as e:
            logger.debug(f"_read_cpu_temp_windows_acpi error: {e}")

        return self._cpu_temp_fallback_cache

    def _cpu_load_limit_by_temp_setting(self) -> int:
        """
        Fallback-лимит CPU, если датчик температуры недоступен.
        Чем ниже пользовательский порог температуры, тем ниже допустимая загрузка CPU.
        """
        try:
            temp_limit = int(self.temp_cpu_max.get())
        except Exception:
            temp_limit = DEFAULT_CPU_TEMP_MAX
        temp_limit = clamp(temp_limit, Config.CPU_TEMP_MIN_C, Config.CPU_TEMP_MAX_C)
        # 60°C -> ~45%, 70°C -> ~55%, 80°C -> ~65%, 90°C -> ~75%
        return clamp(temp_limit - 15, 45, 75)

    def _gpu_load_limit_by_temp_setting(self) -> int:
        """
        Fallback-лимит GPU по загрузке, если датчик температуры GPU недоступен.
        Чем ниже порог температуры GPU, тем ниже допустимая загрузка GPU.
        """
        try:
            temp_limit = int(self.temp_gpu_max.get())
        except Exception:
            temp_limit = DEFAULT_GPU_TEMP_MAX
        temp_limit = clamp(temp_limit, Config.GPU_TEMP_MIN_C, Config.GPU_TEMP_MAX_C)
        # 60°C -> ~50%, 70°C -> ~60%, 75°C -> ~65%, 85°C -> ~75%
        return clamp(temp_limit - 10, 50, 80)

    def _find_face_task_model_path(self) -> Optional[str]:
        """Ищет модель для MediaPipe Tasks FaceDetector рядом с приложением."""
        base = os.path.dirname(os.path.abspath(__file__))
        candidates = []
        for name in Config.FACE_TASK_MODELS:
            candidates.append(os.path.join(base, name))
            candidates.append(os.path.join(base, "models", name))
            candidates.append(os.path.join(base, "assets", name))
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    def _find_face_gpu_model_path(self) -> Optional[str]:
        """Ищет ONNX-модель YuNet для GPU-детектора лиц (OpenCV DNN CUDA)."""
        base = os.path.dirname(os.path.abspath(__file__))
        candidates = []
        for name in Config.FACE_GPU_MODELS:
            candidates.append(os.path.join(base, name))
            candidates.append(os.path.join(base, "models", name))
            candidates.append(os.path.join(base, "assets", name))
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # ТЕМПЕРАТУРНЫЙ ПРЕДОХРАНИТЕЛЬ
    # ══════════════════════════════════════════════════════════════════════════

    def _wait_for_cool(self, get_temp_fn: Callable, limit: int, name: str, timeout: int = Config.TEMP_TIMEOUT) -> None:
        """
        Универсальное ожидание охлаждения с таймаутом.
        timeout — максимум секунд ожидания (дефолт 5 минут).
        Если датчик завис или таймаут истёк — просто продолжаем.
        """
        if self.stop_flag.is_set():
            return
        cool_target = limit - Config.TEMP_COOLDOWN
        start = time.time()
        while time.time() - start < timeout:
            if self.stop_flag.is_set():
                return
            if not self._wait_if_paused():
                return
            time.sleep(3)
            try:
                t = get_temp_fn()
            except Exception as e:
                logger.warning(f"Датчик {name} упал: {e} — продолжаем")
                break
            if t is None or t <= cool_target:
                msg = f"✅ {name} остыл до {t}°C — продолжаем"
                self._safe_log(msg, "success")
                logger.info(msg)
                return
            msg = f"️ Жду... {name} {t}°C (цель ≤ {cool_target}°C)"
            self._safe_log(msg, "warn")
            logger.info(msg)
        msg = f"⚠️ {name} таймаут охлаждения ({timeout}с) — продолжаем принудительно"
        self._safe_log(msg, "warn")
        logger.warning(msg)

    def _check_and_wait_temp(self) -> None:
        """
        Проверяем температуру GPU и CPU.
        Если превышен порог — ждём охлаждения (максимум 5 минут).
        Если датчик температуры GPU недоступен — используем fallback по загрузке GPU.
        Вызывается из рабочего потока — не блокирует UI.
        """
        if self.stop_flag.is_set():
            return

        # Проверка GPU
        try:
            gpu_temp  = GPU.temperature()
            gpu_limit = self.temp_gpu_max.get()
            if gpu_temp is not None and gpu_temp >= gpu_limit:
                msg = f"️ GPU {gpu_temp}°C ≥ {gpu_limit}°C — пауза для охлаждения..."
                self._safe_log(msg, "warn")
                logger.warning(msg)
                self._wait_for_cool(GPU.temperature, gpu_limit, "GPU")
            elif gpu_temp is None and GPU.available and bool(self.use_gpu.get()):
                if not self._warned_no_gpu_temp_sensor:
                    self._warned_no_gpu_temp_sensor = True
                    msg = ("⚠️ Датчик температуры GPU недоступен — "
                           "использую ограничение по загрузке GPU")
                    self._safe_log(msg, "warn")
                    logger.warning(msg)
                self.control_gpu_load(max_gpu=self._gpu_load_limit_by_temp_setting())
        except Exception as e:
            logger.debug(f"Ошибка проверки температуры GPU: {e}")

        # Проверка CPU
        try:
            cpu_limit = self.temp_cpu_max.get()
            cpu_temp = self._read_cpu_temp()
            if cpu_temp is None:
                if not self._warned_no_cpu_temp_sensor:
                    self._warned_no_cpu_temp_sensor = True
                    msg = ("⚠️ Датчик температуры CPU недоступен — "
                           "использую ограничение по загрузке CPU")
                    self._safe_log(msg, "warn")
                    logger.warning(msg)
                self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())
                return
            if cpu_temp >= cpu_limit:
                msg = f"️ CPU {cpu_temp:.0f}°C ≥ {cpu_limit}°C — пауза..."
                self._safe_log(msg, "warn")
                logger.warning(msg)
                self._wait_for_cool(self._read_cpu_temp, cpu_limit, "CPU")
        except Exception as e:
            logger.debug(f"Ошибка проверки температуры CPU: {e}")

    def control_load(self, max_cpu: int = 75) -> None:
        """
        ЕДИНАЯ система контроля нагрузки — заменяет все _throttle вызовы.
        Адаптивная: если CPU < 60% — не ждём, если > 80% — ждём дольше.
        Использовать везде вместо _throttle().
        """
        if self.stop_flag.is_set():
            return
        max_cpu = int(clamp(max_cpu, 35, 95))
        max_cpu = min(max_cpu, self._cpu_load_limit_by_temp_setting())
        try:
            import psutil
            # Читаем реальный CPU
            cpu = psutil.cpu_percent(interval=0.4)
            soft_limit = max(25, max_cpu - 10)

            if cpu <= soft_limit:
                # Нагрузка низкая — продолжаем
                return
            if cpu <= max_cpu:
                # Нагрузка близка к лимиту — короткая пауза
                if not self._wait_if_paused():
                    return
                time.sleep(0.2)
                return

            # Высокая нагрузка — ждём пока не упадёт ниже max_cpu
            logger.debug(f"control_load: CPU {cpu:.0f}% > {max_cpu}% — жду...")
            deadline = time.time() + 30  # максимум 30 секунд ждём
            while time.time() < deadline:
                if self.stop_flag.is_set():
                    return
                if not self._wait_if_paused():
                    return
                time.sleep(0.2)
                cpu = psutil.cpu_percent(interval=0.4)
                if cpu <= max_cpu:
                    break
        except ImportError:
            # psutil недоступен — базовая пауза
            time.sleep(0.3)
        except Exception as e:
            logger.debug(f"control_load ошибка: {e}")
            time.sleep(0.3)

    def control_gpu_load(self, max_gpu: int = 75) -> None:
        """
        Мягкий runtime-лимитер по загрузке GPU (fallback, когда нет датчика температуры GPU).
        Не блокирует надолго: ждёт максимум около 20 секунд.
        """
        if self.stop_flag.is_set():
            return
        if not GPU.available or not bool(self.use_gpu.get()):
            return
        max_gpu = int(clamp(max_gpu, 40, 95))
        try:
            util = GPU.utilization_pct()
            if util is None or util <= max_gpu:
                return
            logger.debug(f"control_gpu_load: GPU {util:.0f}% > {max_gpu}% — жду...")
            deadline = time.time() + 20
            while time.time() < deadline:
                if self.stop_flag.is_set():
                    return
                if not self._wait_if_paused():
                    return
                time.sleep(0.25)
                util = GPU.utilization_pct()
                if util is None or util <= max_gpu:
                    return
        except Exception as e:
            logger.debug(f"control_gpu_load ошибка: {e}")

    def _throttle(self):
        """Обёртка для совместимости — вызывает control_load."""
        self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())

    # ══════════════════════════════════════════════════════════════════════════
    # VRAM ЗАЩИТА
    # ══════════════════════════════════════════════════════════════════════════

    def _check_vram(self, model_mb: int, model_name: str = "модель") -> str:
        """
        Проверяет хватает ли VRAM для загрузки модели с учётом лимита.
        Возвращает device: "cuda" или "cpu".
        """
        if not GPU.available or not self.use_gpu.get():
            return "cpu"
        limit_pct = self.gpu_vram_limit.get()
        if GPU.can_fit_model(model_mb, limit_pct):
            logger.info(f"{model_name}: используем GPU (CUDA)")
            return "cuda"
        else:
            free    = GPU.vram_free_mb() or 0
            allowed = int(GPU.vram_total * limit_pct / 100)
            msg = (f"⚠️ {model_name}: нет VRAM (нужно ~{model_mb}MB, "
                   f"доступно {free}MB из лимита {allowed}MB) — переключаю на CPU")
            self._safe_log(msg, "warn")
            logger.warning(msg)
            return "cpu"

    # ══════════════════════════════════════════════════════════════════════════
    # ВСПОМОГАТЕЛЬНЫЕ UI МЕТОДЫ
    # ══════════════════════════════════════════════════════════════════════════

    def _safe_log(self, msg: str, tag: str = "info") -> None:
        self.after(0, lambda: self._log(msg, tag))

    def _safe_progress(self, val: float, text: str = "") -> None:
        self.after(0, lambda: self._set_progress(val, text))

    def _log(self, msg: str, tag: str = "info") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_progress(self, val: float, text: str = "") -> None:
        self.progress.set(val)
        self.lbl_progress.configure(text=text)

    def _wait_if_paused(self) -> bool:
        """
        Блокирует рабочий поток пока нажата пауза.
        Возвращает False если нужно остановиться (stop_flag).
        """
        return self._worker.wait_if_paused()

    def _grad_line(self, parent, height=2):
        # Лента в стиле Creator Studio: тёплый акцент + холодная подложка
        line = tk.Frame(parent, bg=BG)
        line.pack(fill="x")
        tk.Frame(line, height=1, bg=RED2).pack(fill="x")
        if height > 1:
            tk.Frame(line, height=max(1, height - 1), bg=BG4).pack(fill="x")
        return line

    def _card(self, parent, title, icon=""):
        outer = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        outer.pack(fill="x", pady=(0, 14))
        shell = tk.Frame(outer, bg=BG2)
        shell.pack(fill="x")
        tk.Frame(shell, bg=PURPLE, height=2).pack(fill="x")
        hdr = tk.Frame(shell, bg=BG2, padx=14, pady=10)
        hdr.pack(fill="x")
        t = f"{icon}  {title.upper()}" if icon else title.upper()
        tk.Label(hdr, text=t, font=FONT_LABEL, bg=BG2, fg=TEXT).pack(side="left")
        tk.Label(hdr, text="CREATOR STUDIO", font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(side="right")
        tk.Frame(shell, bg=BORDER, height=1).pack(fill="x")
        body = tk.Frame(shell, bg=BG2, padx=14, pady=12)
        body.pack(fill="x")
        body.columnconfigure(0, weight=1)
        return body

    def _btn(self, parent, text, cmd, bg=BG3, fg=TEXT, **kw):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         font=FONT_BTN, bd=0, padx=14, pady=8, relief="flat",
                         activebackground=PURPLE2, activeforeground=BG,
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=PURPLE2,
                         cursor="hand2", **kw)

    def _scrollable(self, parent):
        # Простой Frame вместо Canvas+Scrollbar — убирает чёрный экран на Windows
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        pad = tk.Frame(wrap, bg=BG, padx=24, pady=18)
        pad.pack(fill="both", expand=True)
        pad.columnconfigure((0, 1), weight=1)
        return pad

    # ══════════════════════════════════════════════════════════════════════════
    # ПОСТРОЕНИЕ UI
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        root = tk.Frame(self, bg=BG); root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1); root.rowconfigure(2, weight=1)

        self._grad_line(root, height=3)
        self._build_header(root)
        self._grad_line(root, height=1)

        self._tab_defs = [
            ("video",    "ВИДЕО"),
            ("settings", "НАСТРОЙКИ"),
            ("effects",  "ЭФФЕКТЫ"),
            ("ai",       "АНАЛИЗ"),
            ("perf",     "ПРОИЗВОДИТЕЛЬНОСТЬ"),
        ]
        self.tabbar = TabBar(root, self._tab_defs, self._on_tab_change)
        self.tabbar.pack(fill="x")
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

        # Нижнюю панель строим до tab_container, чтобы pack не сплющивал её высоту.
        self._build_bottom(root)
        print(">>> bottom OK", flush=True)

        self.tab_container = tk.Frame(root, bg=BG)
        self.tab_container.pack(fill="both", expand=True, side="top")
        self.tab_container.columnconfigure(0, weight=1)
        self.tab_container.rowconfigure(0, weight=1)

        self.tabs = {}
        for key, _ in self._tab_defs:
            f = tk.Frame(self.tab_container, bg=BG)
            f.grid(row=0, column=0, sticky="nsew")
            self.tabs[key] = f

        self._build_tab_video(self.tabs["video"])
        print(">>> tab_video OK", flush=True)
        self._build_tab_settings(self.tabs["settings"])
        print(">>> tab_settings OK", flush=True)
        self._build_tab_effects(self.tabs["effects"])
        print(">>> tab_effects OK", flush=True)
        self._build_tab_ai(self.tabs["ai"])
        print(">>> tab_ai OK", flush=True)
        self._build_tab_perf(self.tabs["perf"])
        print(">>> tab_perf OK", flush=True)
        self.ui_expert_mode.trace_add("write", lambda *a: self._apply_ui_mode())
        self._apply_ui_mode()
        print(">>> _build_ui ЗАВЕРШЁН", flush=True)

    # ── Шапка ─────────────────────────────────────────────────────────────────

    def _build_header(self, parent):
        hdr = tk.Frame(parent, bg=BG, padx=22, pady=12)
        hdr.pack(fill="x")
        hdr.columnconfigure(1, weight=1)

        left = tk.Frame(hdr, bg=BG)
        left.grid(row=0, column=0, sticky="w")
        self.lbl_title = tk.Label(left, text="CLIP AI",
                                  font=("Bahnschrift SemiBold", 24), bg=BG, fg=RED2)
        self.lbl_title.pack(side="left")
        tk.Label(left, text="  v2.0  creator studio",
                 font=("Segoe UI Semibold", 10), bg=BG, fg=TEXT_DIM).pack(side="left", pady=(6, 0))

        mode_wrap = tk.Frame(left, bg=BG)
        mode_wrap.pack(side="left", padx=(14, 0), pady=(6, 0))
        self.btn_ui_mode = tk.Button(
            mode_wrap, text="", command=self._toggle_ui_mode,
            font=FONT_SMALL, bg=BG3, fg=SUCCESS, bd=0, padx=10, pady=4,
            activebackground=BG4, activeforeground=TEXT,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=PURPLE2,
            cursor="hand2", relief="flat"
        )
        self.btn_ui_mode.pack(anchor="w")
        self.lbl_ui_mode_hint = tk.Label(mode_wrap, text="", font=FONT_SMALL, bg=BG, fg=TEXT_DIM)
        self.lbl_ui_mode_hint.pack(anchor="w")
        self._refresh_ui_mode_widgets()

        right_outer = tk.Frame(hdr, bg=BORDER, padx=1, pady=1)
        right_outer.grid(row=0, column=2, sticky="e")
        right = tk.Frame(right_outer, bg=BG3, padx=12, pady=7)
        right.pack(fill="both", expand=True)

        if GPU.available:
            tk.Label(right, text=f" {GPU.short_name()}",
                     font=FONT_SMALL, bg=BG3, fg=SUCCESS).pack(anchor="e")
        else:
            tk.Label(right, text="GPU unavailable",
                     font=FONT_SMALL, bg=BG3, fg=WARN).pack(anchor="e")

        self.lbl_cpu_temp = tk.Label(right, text="CPU --°C",
                                     font=FONT_SMALL, bg=BG3, fg=TEXT_DIM)
        self.lbl_cpu_temp.pack(anchor="e")
        self.lbl_gpu_temp = tk.Label(right, text="GPU --°C",
                                     font=FONT_SMALL, bg=BG3, fg=TEXT_DIM)
        self.lbl_gpu_temp.pack(anchor="e")
        self.lbl_vram = tk.Label(right, text="VRAM --",
                                 font=FONT_SMALL, bg=BG3, fg=TEXT_DIM)
        self.lbl_vram.pack(anchor="e")

        colors = [RED2, PURPLE, PURPLE2, PINK, PURPLE]
        idx = [0]

        def anim():
            try:
                self.lbl_title.configure(fg=colors[idx[0] % len(colors)])
                idx[0] += 1
                self.after(1100, anim)
            except Exception as e:
                logger.debug(f"Title animation stopped: {e}")

        self.after(1100, anim)

    def _visible_tab_keys(self) -> List[str]:
        if bool(self.ui_expert_mode.get()):
            return ["video", "settings", "effects", "ai", "perf"]
        return ["video", "settings", "perf"]

    def _toggle_ui_mode(self):
        self.ui_expert_mode.set(not bool(self.ui_expert_mode.get()))

    def _refresh_ui_mode_widgets(self):
        expert = bool(self.ui_expert_mode.get())
        if hasattr(self, "btn_ui_mode"):
            self.btn_ui_mode.configure(
                text="UI: Расширенный" if expert else "UI: Простой",
                fg=PURPLE2 if expert else SUCCESS,
            )
        if hasattr(self, "lbl_ui_mode_hint"):
            self.lbl_ui_mode_hint.configure(
                text="Все вкладки доступны" if expert else "Скрыты Эффекты и Анализ",
            )
        if hasattr(self, "btn_ui_mode_settings"):
            self.btn_ui_mode_settings.configure(
                text="Переключить на Простой интерфейс" if expert else "Переключить на Расширенный интерфейс",
            )

    def _apply_ui_mode(self):
        visible = self._visible_tab_keys()
        if hasattr(self, "tabbar"):
            self.tabbar.set_visible(visible)
            active = self.tabbar.active.get()
            if active not in visible:
                active = "video" if "video" in visible else visible[0]
                self.tabbar.active.set(active)
            self.tabbar._update()
            self._on_tab_change(active)
        self._refresh_ui_mode_widgets()

    def _on_tab_change(self, key):
        if key in self.tabs:
            self.tabs[key].tkraise()

    # ── Вкладка: Видео ────────────────────────────────────────────────────────

    def _build_tab_video(self, parent):
        pad = self._scrollable(parent)
        pad.columnconfigure(0, weight=3)
        pad.columnconfigure(1, weight=2)

        top = tk.Frame(pad, bg=BG)
        top.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 8))
        left = tk.Frame(pad, bg=BG)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(pad, bg=BG)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 0))

        body = self._card(top, "Очередь видео", "🎬")
        tk.Label(
            body,
            text="Добавь локальные видео в очередь. Обработка пойдет сверху вниз по списку.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=620, justify="left"
        ).pack(anchor="w", pady=(0, 8))
        lbf = tk.Frame(body, bg=BG3, pady=2)
        lbf.pack(fill="x", pady=(0, 10))
        self.queue_listbox = tk.Listbox(
            lbf, bg=BG3, fg=TEXT, font=FONT_MONO, height=8,
            selectbackground=PURPLE, selectforeground=BG, bd=0,
            highlightthickness=0, activestyle="none"
        )
        self.queue_listbox.pack(fill="x", padx=6, pady=6)
        row = tk.Frame(body, bg=BG2)
        row.pack(fill="x")
        self._btn(row, "+ Добавить видео", self._add_video, bg=PURPLE, fg=BG).pack(side="left", padx=(0, 8))
        self._btn(row, "Убрать выбранное", self._remove_video, bg=BG3, fg=DANGER).pack(side="left")

        body2 = self._card(left, "Папка для клипов", "📁")
        tk.Label(
            body2,
            text="Сюда будут сохранены готовые клипы.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM
        ).pack(anchor="w", pady=(0, 6))
        row2 = tk.Frame(body2, bg=BG3)
        row2.pack(fill="x")
        row2.columnconfigure(0, weight=1)
        tk.Label(
            row2, textvariable=self.out_folder, font=FONT_MONO,
            bg=BG3, fg=TEXT_MED, anchor="w", wraplength=360, justify="left"
        ).pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self._btn(row2, "Выбрать", self._pick_folder, bg=BG4, fg=PURPLE2).pack(side="right", padx=6, pady=6)

        body3 = self._card(right, "Импорт с YouTube", "⬇")
        tk.Label(
            body3,
            text="Вставь ссылку и скачай ролик прямо в очередь.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=320, justify="left"
        ).pack(anchor="w", pady=(0, 6))
        yt = tk.Frame(body3, bg=BG3)
        yt.pack(fill="x")
        self.yt_entry = tk.Entry(
            yt, textvariable=self.yt_url, font=FONT_MONO,
            bg=BG3, fg=TEXT_DIM, insertbackground=PURPLE2,
            bd=0, highlightthickness=0
        )
        self.yt_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=8, pady=4)
        self.yt_entry.insert(0, "https://youtube.com/watch?v=...")
        self.yt_entry.bind("<FocusIn>", self._yt_in)
        self.yt_entry.bind("<FocusOut>", self._yt_out)
        self._btn(yt, "Скачать", self._download_yt, bg=BG4, fg=PURPLE2).pack(side="right", padx=6, pady=6)

    def _build_tab_settings(self, parent):
        pad = self._scrollable(parent)
        pad.columnconfigure(0, weight=2)
        pad.columnconfigure(1, weight=3)
        left = tk.Frame(pad, bg=BG); left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(pad, bg=BG); right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        body = self._card(left, "Параметры клипов", "⚙️")
        grid = tk.Frame(body, bg=BG2); grid.pack(fill="x")
        grid.columnconfigure((0, 1, 2), weight=1)
        for col, (label, var, f, t) in enumerate([
            ("Клипов", self.num_clips, 1, 60),
            ("Мин.сек", self.min_dur, 10, 120),
            ("Макс.сек", self.max_dur, 20, 300),
        ]):
            cell = tk.Frame(grid, bg=BG3, padx=8, pady=8)
            cell.grid(row=0, column=col, padx=(0 if col == 0 else 4, 0), sticky="ew")
            tk.Label(cell, text=label, font=FONT_SMALL, bg=BG3, fg=TEXT_DIM).pack()
            NumStepper(cell, var, f, t).pack(pady=(4, 0))

        body2 = self._card(right, "Режим анализа", "⚡")
        tk.Label(
            body2,
            text="Режим ниже относится только к AI-анализу (поиск моментов).",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=320
        ).pack(anchor="w", pady=(0, 6))

        def _analysis_components_for_mode(mode_key: str) -> str:
            m = str(mode_key).strip().lower()
            gpu_ai_available = bool(GPU.available) and bool(self.use_gpu.get())
            if m == "split":
                return "нет (режим По времени)"

            comps = ["громкость"]
            if m in ("balanced", "full", "hybrid"):
                if self.opt_laugh.get():
                    comps.append("смех(CPU)")
                if self.opt_motion.get():
                    move_where = "GPU" if self._is_motion_gpu_enabled() else "CPU"
                    comps.append(f"движение({move_where})")
            if m in ("full", "hybrid"):
                if self.opt_whisper.get():
                    if m == "hybrid":
                        comps.append("речь(GPU/CPU авто)")
                    else:
                        comps.append("речь(CPU)")
                if self.opt_faces.get():
                    if gpu_ai_available:
                        comps.append("лица(GPU/CPU авто)")
                    else:
                        comps.append("лица(CPU)")

            return ", ".join(comps)

        def _mode_analysis_short(mode_key: str) -> str:
            m = str(mode_key).strip().lower()
            gpu_ai_available = bool(GPU.available) and bool(self.use_gpu.get())
            motion_gpu = bool(self.opt_motion.get()) and self._is_motion_gpu_enabled()
            if m == "fast":
                return "CPU"
            if m == "balanced":
                return "CPU+GPU" if motion_gpu else "CPU"
            if m == "hybrid":
                hybrid_gpu = motion_gpu or (gpu_ai_available and (self.opt_whisper.get() or self.opt_faces.get()))
                return "CPU+GPU" if hybrid_gpu else "CPU"
            if m == "full":
                full_gpu = motion_gpu or (self.opt_faces.get() and gpu_ai_available)
                return "CPU+GPU" if full_gpu else "CPU"
            if m == "split":
                return "выкл"
            return "CPU"

        def _render_components_now() -> str:
            effects = []
            if self.opt_mirror.get():
                effects.append("зеркало")
            if self.opt_zoom.get():
                effects.append("зум 2%")
            if self.opt_color.get():
                effects.append("цвет")
            if self.opt_vertical.get():
                effects.append("9:16")
            speed = str(self.speed_val.get()).strip() or "1x"
            effects.append(f"скорость {speed}")
            if self.opt_subs.get():
                effects.append("субтитры")
            if not effects:
                return "нарезка + кодек + аудио (без эффектов)"
            return f"нарезка + кодек + аудио; эффекты: {', '.join(effects)}"

        def _mode_runtime_text(mode_key: str) -> str:
            analysis = _mode_analysis_short(mode_key)
            render = "GPU" if self._effective_fx_backend() == "gpu" else "CPU"
            analysis_components = _analysis_components_for_mode(mode_key)
            render_components = _render_components_now()
            return (
                f"Анализ: {analysis} | компоненты: {analysis_components}\n"
                f"Рендер: {render} | {render_components}"
            )

        modes = [
            ("fast",     "⚡ Быстрый",    "только громкость"),
            ("balanced", "⚖️ Баланс",     "громкость + движение"),
            ("hybrid",   " Гибрид",     "CPU + GPU параллельно"),
            ("full",     " Полный",     "полный AI; лица могут идти через GPU"),
            ("split",    "✂️ По времени", "нарезка без AI"),
        ]
        for val, label, hint in modes:
            f = tk.Frame(body2, bg=BG3, cursor="hand2"); f.pack(fill="x", pady=(0, 5))
            f.bind("<Button-1>", lambda e, v=val: self.mode.set(v))
            inner = tk.Frame(f, bg=BG3, padx=10, pady=7); inner.pack(fill="x")
            inner.bind("<Button-1>", lambda e, v=val: self.mode.set(v))
            inner.columnconfigure(1, weight=1)
            ind = tk.Label(inner, text="○", font=("Bahnschrift SemiBold", 12), bg=BG3, fg=PURPLE2, cursor="hand2")
            ind.grid(row=0, column=0, padx=(0, 8))
            ind.bind("<Button-1>", lambda e, v=val: self.mode.set(v))
            info = tk.Frame(inner, bg=BG3); info.grid(row=0, column=1, sticky="w")
            info.bind("<Button-1>", lambda e, v=val: self.mode.set(v))
            tk.Label(info, text=label, font=FONT_BTN, bg=BG3, fg=TEXT).pack(anchor="w")
            tk.Label(info, text=hint, font=FONT_SMALL, bg=BG3, fg=TEXT_DIM).pack(anchor="w")
            load_lbl = tk.Label(
                inner, text="", font=FONT_SMALL, bg=BG3, fg=TEXT_MED,
                justify="right", wraplength=300
            )
            load_lbl.grid(row=0, column=2, padx=(8, 0))
            def upd(ind=ind, f=f, inner=inner, info=info, v=val, load_lbl=load_lbl):
                active = self.mode.get() == v
                clr = BG4 if active else BG3
                load = _mode_runtime_text(v)
                lc = PURPLE2 if "GPU" in load else TEXT_MED
                ind.configure(text="◉" if active else "○", bg=clr)
                f.configure(bg=clr); inner.configure(bg=clr); info.configure(bg=clr)
                for w in info.winfo_children(): w.configure(bg=clr)
                load_lbl.configure(text=load, fg=lc, bg=clr)
            self.mode.trace_add("write", lambda *a, u=upd: u())
            self.fx_backend.trace_add("write", lambda *a, u=upd: u())
            self.use_gpu.trace_add("write", lambda *a, u=upd: u())
            self.opt_whisper.trace_add("write", lambda *a, u=upd: u())
            self.opt_laugh.trace_add("write", lambda *a, u=upd: u())
            self.opt_motion.trace_add("write", lambda *a, u=upd: u())
            self.opt_faces.trace_add("write", lambda *a, u=upd: u())
            self.opt_mirror.trace_add("write", lambda *a, u=upd: u())
            self.opt_zoom.trace_add("write", lambda *a, u=upd: u())
            self.opt_color.trace_add("write", lambda *a, u=upd: u())
            self.opt_vertical.trace_add("write", lambda *a, u=upd: u())
            self.opt_subs.trace_add("write", lambda *a, u=upd: u())
            self.speed_val.trace_add("write", lambda *a, u=upd: u())
            upd()

        tk.Label(
            body2,
            text="Подпись справа обновляется автоматически при смене режима и CPU/GPU-переключателей.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=320
        ).pack(anchor="w", pady=(2, 4))

        # Настройки режима По времени
        self.split_frame = tk.Frame(body2, bg=BG2)
        self.split_frame.pack(fill="x", pady=(4, 0))
        tk.Frame(self.split_frame, bg=BORDER, height=1).pack(fill="x", pady=(0, 8))
        sr1 = tk.Frame(self.split_frame, bg=BG2); sr1.pack(fill="x", pady=(0, 6))
        tk.Label(sr1, text="Длина клипа (сек):", font=FONT_SMALL, bg=BG2, fg=TEXT_MED).pack(side="left", padx=(0, 8))
        NumStepper(sr1, self.split_dur, 10, 300).pack(side="left")
        sr2 = tk.Frame(self.split_frame, bg=BG2); sr2.pack(fill="x")
        Toggle(sr2, self.split_limit, "Ограничить кол-во клипов").pack(anchor="w")

        def _upd_split(*a):
            if self.mode.get() == "split":
                self.split_frame.pack(fill="x", pady=(4, 0))
            else:
                self.split_frame.pack_forget()
        self.mode.trace_add("write", _upd_split); _upd_split()

        body_ui = self._card(right, "Интерфейс", "🧭")
        tk.Label(
            body_ui,
            text="Простой режим скрывает вкладки Эффекты и Анализ, "
                 "чтобы не перегружать экран.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=320, justify="left"
        ).pack(anchor="w", pady=(0, 8))
        self.btn_ui_mode_settings = self._btn(
            body_ui, "", self._toggle_ui_mode, bg=BG3, fg=TEXT
        )
        self.btn_ui_mode_settings.pack(fill="x")
        self._refresh_ui_mode_widgets()

    # ── Вкладка: Эффекты ──────────────────────────────────────────────────────

    def _build_tab_effects(self, parent):
        pad = self._scrollable(parent)
        pad.columnconfigure(0, weight=3)
        pad.columnconfigure(1, weight=2)
        left = tk.Frame(pad, bg=BG); left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(pad, bg=BG); right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        body = self._card(left, "Визуальные эффекты", "✨")
        for label, var in [
            (" Зеркало (hflip)", self.opt_mirror),
            (" Зум 2%", self.opt_zoom),
            (" Цветокоррекция", self.opt_color),
            (" Вертикальный 9:16", self.opt_vertical),
        ]:
            row = tk.Frame(body, bg=BG3, padx=10, pady=8); row.pack(fill="x", pady=(0, 5))
            Toggle(row, var, label).pack(anchor="w")

        body2 = self._card(left, "Скорость видео", "⏩")
        tk.Label(body2, text="Видео и аудио меняются синхронно",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w", pady=(0, 8))
        speeds = [("0.5x", "Замедление"), ("1x", "Обычная"), ("1.5x", "Ускорение"), ("2x", "Быстро")]
        sg = tk.Frame(body2, bg=BG2); sg.pack(fill="x")
        sg.columnconfigure((0, 1, 2, 3), weight=1)
        for i, (val, label) in enumerate(speeds):
            cell = tk.Frame(sg, bg=BG3, padx=6, pady=8, cursor="hand2")
            cell.grid(row=0, column=i, padx=(0 if i == 0 else 4, 0), sticky="ew")
            cell.bind("<Button-1>", lambda e, v=val: self.speed_val.set(v))
            lbl = tk.Label(cell, text=val, font=FONT_BTN, bg=BG3, fg=TEXT, cursor="hand2")
            lbl.pack(); lbl.bind("<Button-1>", lambda e, v=val: self.speed_val.set(v))
            hl = tk.Label(cell, text=label, font=FONT_SMALL, bg=BG3, fg=TEXT_DIM, cursor="hand2")
            hl.pack(); hl.bind("<Button-1>", lambda e, v=val: self.speed_val.set(v))
            def upd_s(cell=cell, lbl=lbl, hl=hl, v=val):
                active = self.speed_val.get() == v
                clr = BG4 if active else BG3; ac = PURPLE2 if active else TEXT
                cell.configure(bg=clr); lbl.configure(bg=clr, fg=ac); hl.configure(bg=clr)
            self.speed_val.trace_add("write", lambda *a, u=upd_s: u()); upd_s()

        body_backend = self._card(right, "Где считать эффекты", "⚙")
        tk.Label(
            body_backend,
            text="Функции: Зеркало, Зум 2%, Цветокоррекция, Вертикальный 9:16, Скорость видео",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM
        ).pack(anchor="w", pady=(0, 8))
        tk.Label(
            body_backend,
            text="Этот выбор влияет на Шаг 2 (рендер) и на компонент Движение в AI-анализе.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=280
        ).pack(anchor="w", pady=(0, 6))
        bg_sel = tk.Frame(body_backend, bg=BG2)
        bg_sel.pack(fill="x")
        bg_sel.columnconfigure((0, 1), weight=1)
        options = [
            ("cpu", "CPU", "Стабильно и предсказуемо"),
            ("gpu", "GPU", "Быстрее рендер (NVENC)"),
        ]
        for i, (value, title, hint) in enumerate(options):
            cell = tk.Frame(bg_sel, bg=BG3, padx=8, pady=8, cursor="hand2")
            cell.grid(row=0, column=i, padx=(0 if i == 0 else 4, 0), sticky="ew")
            lbl = tk.Label(cell, text=title, font=FONT_BTN, bg=BG3, fg=TEXT, cursor="hand2")
            lbl.pack()
            hl = tk.Label(cell, text=hint, font=FONT_SMALL, bg=BG3, fg=TEXT_DIM, cursor="hand2")
            hl.pack()

            def pick_backend(v=value):
                if v == "gpu" and not GPU.available:
                    self.fx_backend.set("cpu")
                    self._safe_log("⚠️ GPU недоступен — Зеркало/Зум/Цвет/9:16/Скорость остаются на CPU", "warn")
                    return
                self.fx_backend.set(v)

            cell.bind("<Button-1>", lambda e, p=pick_backend: p())
            lbl.bind("<Button-1>", lambda e, p=pick_backend: p())
            hl.bind("<Button-1>", lambda e, p=pick_backend: p())

            def upd_fx(cell=cell, lbl=lbl, hl=hl, v=value):
                active = self.fx_backend.get() == v
                locked = (v == "gpu" and not GPU.available)
                clr = BG4 if active and not locked else BG3
                txt = TEXT_DIM if locked else (PURPLE2 if active else TEXT)
                sub = WARN if locked else TEXT_DIM
                cell.configure(bg=clr)
                lbl.configure(bg=clr, fg=txt)
                hl.configure(bg=clr, fg=sub)

            self.fx_backend.trace_add("write", lambda *a, u=upd_fx: u())
            upd_fx()

        tk.Label(
            body_backend,
            text="Если общий тумблер GPU в Производительности выключен, применяется CPU.",
            font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=280
        ).pack(anchor="w", pady=(6, 0))

        body3 = self._card(right, "Субтитры", "")
        Toggle(body3, self.opt_subs, "Покадровые субтитры (слово за словом)").pack(anchor="w", pady=(0, 8))
        tk.Label(body3, text="Стиль: белый текст с чёрной обводкой по центру",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w")
        tk.Label(body3, text="Требует режим Гибрид или Полный (нужен Whisper)",
                 font=FONT_SMALL, bg=BG2, fg=WARN, wraplength=280).pack(anchor="w", pady=(4, 0))
        tk.Label(body3, text="✅ С зеркалом работает корректно — фильтры в правильном порядке",
                 font=FONT_SMALL, bg=BG2, fg=SUCCESS, wraplength=280).pack(anchor="w", pady=(4, 0))

    # ── Вкладка: Анализ AI ────────────────────────────────────────────────────

    def _build_tab_ai(self, parent):
        pad = self._scrollable(parent)
        pad.columnconfigure(0, weight=3)
        pad.columnconfigure(1, weight=2)
        left = tk.Frame(pad, bg=BG); left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(pad, bg=BG); right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        body = self._card(left, "AI компоненты", "")
        for comp_key, label, var, wvar, where in [
            ("speech", " Речь (Whisper)", self.opt_whisper, self.w_speech, "GPU"),
            ("laugh",  " Смех",           self.opt_laugh,   self.w_laugh,  "CPU"),
            ("motion", " Движение",       self.opt_motion,  self.w_motion, "CPU"),
            ("faces",  " Лица",           self.opt_faces,   self.w_faces,  "GPU"),
        ]:
            row = tk.Frame(body, bg=BG3, padx=10, pady=7); row.pack(fill="x", pady=(0, 5))
            row.columnconfigure(1, weight=1)
            Toggle(row, var).grid(row=0, column=0, padx=(0, 8))
            tk.Label(row, text=label, font=FONT_SMALL, bg=BG3, fg=TEXT).grid(row=0, column=1, sticky="w")
            bc = PURPLE2 if where == "GPU" else TEXT_MED
            where_lbl = tk.Label(row, text=where, font=FONT_SMALL, bg=BG4, fg=bc, padx=6, pady=2)
            where_lbl.grid(row=0, column=2, padx=(4, 8))
            if comp_key == "motion":
                def _upd_motion_where(lbl=where_lbl):
                    motion_gpu = self._is_motion_gpu_enabled()
                    txt = "GPU" if motion_gpu else "CPU"
                    fg = PURPLE2 if motion_gpu else TEXT_MED
                    lbl.configure(text=txt, fg=fg)

                self.fx_backend.trace_add("write", lambda *a, u=_upd_motion_where: u())
                self.use_gpu.trace_add("write", lambda *a, u=_upd_motion_where: u())
                self.opt_motion.trace_add("write", lambda *a, u=_upd_motion_where: u())
                _upd_motion_where()
            pri = tk.Frame(row, bg=BG3); pri.grid(row=0, column=3)
            tk.Label(pri, text="вес:", font=FONT_SMALL, bg=BG3, fg=TEXT_DIM).pack(side="left", padx=(0, 4))
            NumStepper(pri, wvar, 1, 5, width=2).pack(side="left")

        row_a = tk.Frame(body, bg=BG3, padx=10, pady=7); row_a.pack(fill="x", pady=(0, 5))
        row_a.columnconfigure(1, weight=1)
        tk.Label(row_a, text=" Аудио громкость", font=FONT_SMALL, bg=BG3, fg=TEXT).grid(row=0, column=1, sticky="w")
        tk.Label(row_a, text="CPU", font=FONT_SMALL, bg=BG4, fg=TEXT_MED,
                 padx=6, pady=2).grid(row=0, column=2, padx=(4, 8))
        pri_a = tk.Frame(row_a, bg=BG3); pri_a.grid(row=0, column=3)
        tk.Label(pri_a, text="вес:", font=FONT_SMALL, bg=BG3, fg=TEXT_DIM).pack(side="left", padx=(0, 4))
        NumStepper(pri_a, self.w_audio, 1, 5, width=2).pack(side="left")

        body2 = self._card(right, "Ключевые слова", "")
        tk.Label(body2, text="Whisper выделит моменты с этими словами",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w", pady=(0, 6))
        kw = tk.Frame(body2, bg=BG3); kw.pack(fill="x")
        tk.Entry(kw, textvariable=self.keywords, font=FONT_MONO, bg=BG3, fg=TEXT,
                 insertbackground=PURPLE2, bd=0, highlightthickness=0).pack(fill="x", ipady=6, padx=8)
        tk.Label(body2, text="Через запятую. Пример: смех, вау, победа",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w", pady=(4, 0))

        body3 = self._card(right, "Гибрид режим", "")
        tk.Label(body3, text="CPU параллельно делает:",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_MED).pack(anchor="w")
        tk.Label(body3, text="  • Анализ смеха\n  • Громкость аудио",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w")

        motion_note = tk.Label(body3, text="",
                               font=FONT_SMALL, bg=BG2, fg=TEXT_DIM,
                               wraplength=280, justify="left")
        motion_note.pack(anchor="w", pady=(4, 0))

        tk.Label(body3, text="GPU параллельно делает:",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_MED).pack(anchor="w", pady=(8, 0))
        tk.Label(body3, text="  • Whisper (речь)\n  • Face detector (лица, GPU/CPU авто)",
                 font=FONT_SMALL, bg=BG2, fg=PURPLE2).pack(anchor="w")

        hybrid_note = tk.Label(body3, text="",
                               font=FONT_SMALL, bg=BG2, fg=SUCCESS,
                               wraplength=280, justify="left")
        hybrid_note.pack(anchor="w", pady=(6, 0))

        def _upd_hybrid_runtime_notes(note_lbl=motion_note, mode_lbl=hybrid_note):
            if not self.opt_motion.get():
                note_lbl.configure(text="  • Анализ движения: выключен", fg=TEXT_DIM)
            elif self._is_motion_gpu_enabled():
                note_lbl.configure(
                    text="  • Анализ движения: GPU (переключатель Эффектов = GPU)",
                    fg=PURPLE2,
                )
            else:
                note_lbl.configure(
                    text="  • Анализ движения: CPU (Эффекты=CPU или GPU недоступен)",
                    fg=TEXT_DIM,
                )

            if self.mode.get() == "hybrid":
                mode_lbl.configure(text="⚡ В Гибриде CPU и GPU идут параллельно — это нормально.")
            else:
                mode_lbl.configure(text="ℹ️ Карточка описывает поведение режима Гибрид.")

        self.fx_backend.trace_add("write", lambda *a, u=_upd_hybrid_runtime_notes: u())
        self.use_gpu.trace_add("write", lambda *a, u=_upd_hybrid_runtime_notes: u())
        self.opt_motion.trace_add("write", lambda *a, u=_upd_hybrid_runtime_notes: u())
        self.mode.trace_add("write", lambda *a, u=_upd_hybrid_runtime_notes: u())
        _upd_hybrid_runtime_notes()

    # ── Вкладка: Производительность ───────────────────────────────────────────

    def _build_tab_perf(self, parent):
        pad = self._scrollable(parent)
        pad.columnconfigure(0, weight=2)
        pad.columnconfigure(1, weight=2)
        left = tk.Frame(pad, bg=BG); left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(pad, bg=BG); right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # ── GPU ──
        body_gpu = self._card(left, "Видеокарта (GPU)", "")
        if GPU.available:
            tk.Label(body_gpu, text=f"✅ {GPU.name}",
                     font=FONT_LABEL, bg=BG2, fg=SUCCESS).pack(anchor="w")
            tk.Label(body_gpu, text=f"VRAM: {GPU.vram_total} MB  |  CUDA: {GPU.cuda_version}",
                     font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w", pady=(2, 8))
            Toggle(body_gpu, self.use_gpu, "Использовать GPU").pack(anchor="w", pady=(0, 10))

            tk.Label(body_gpu, text="Лимит VRAM (%):",
                     font=FONT_SMALL, bg=BG2, fg=TEXT_MED).pack(anchor="w")
            tk.Label(body_gpu,
                     text="Программа проверяет VRAM перед каждой загрузкой модели.\n"
                          "Если не хватает — автоматически переключается на CPU.",
                     font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=290,
                     justify="left").pack(anchor="w", pady=(2, 4))
            tk.Scale(body_gpu, variable=self.gpu_vram_limit, from_=30, to=90,
                     orient="horizontal", bg=BG2, fg=TEXT, troughcolor=BG3,
                     highlightthickness=0, bd=0, sliderlength=16,
                     activebackground=PURPLE, font=FONT_SMALL,
                     command=lambda v: None).pack(fill="x")
            tk.Label(body_gpu,
                     text=f"✅ Дефолт {DEFAULT_GPU_VRAM_PCT}% — безопасно  |  Не выше 90%",
                     font=FONT_SMALL, bg=BG2, fg=SUCCESS).pack(anchor="w", pady=(2, 0))
        else:
            tk.Label(body_gpu, text="⚠️ GPU недоступен",
                     font=FONT_LABEL, bg=BG2, fg=WARN).pack(anchor="w")
            tk.Label(body_gpu,
                     text="Установите CUDA Toolkit и PyTorch с CUDA.\n"
                          "Проверь: nvidia-smi в командной строке.",
                     font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=290).pack(anchor="w", pady=(4, 0))

        # ── CPU ──
        body_cpu = self._card(left, "Процессор (CPU)", "")
        cpu_total = get_cpu_count()
        tk.Label(body_cpu, text=f"Доступно ядер: {cpu_total}",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(anchor="w", pady=(0, 4))
        tk.Label(body_cpu, text="Потоков для анализа:",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_MED).pack(anchor="w")
        tk.Label(body_cpu,
                 text="Оставьте минимум 2 ядра системе — иначе ноут будет тормозить.",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=290).pack(anchor="w", pady=(2, 4))
        tk.Scale(body_cpu, variable=self.cpu_threads, from_=1, to=self._max_safe_cpu_threads(),
                 orient="horizontal", bg=BG2, fg=TEXT, troughcolor=BG3,
                 highlightthickness=0, bd=0, sliderlength=16,
                 activebackground=PURPLE, font=FONT_SMALL,
                 command=lambda v: self._apply_cpu_limit()).pack(fill="x")
        default_t = safe_cpu_threads(DEFAULT_CPU_THREAD_PCT)
        tk.Label(body_cpu,
                 text=f"✅ Дефолт {default_t} из {cpu_total} ({DEFAULT_CPU_THREAD_PCT}% ядер) — безопасно",
                 font=FONT_SMALL, bg=BG2, fg=SUCCESS).pack(anchor="w", pady=(2, 0))

        # ── Температурный предохранитель ──
        body_temp = self._card(right, "Температурный предохранитель", "️")
        tk.Label(body_temp,
                 text="Программа автоматически ставит паузу если температура\n"
                      f"превышает порог, и ждёт охлаждения на {TEMP_COOLDOWN_DELTA}°C ниже порога.",
                 font=FONT_SMALL, bg=BG2, fg=TEXT_DIM, wraplength=290,
                 justify="left").pack(anchor="w", pady=(0, 10))

        for label, var, default, danger, max_allowed in [
            ("Макс. температура CPU (°C):", self.temp_cpu_max, DEFAULT_CPU_TEMP_MAX, 90, Config.CPU_TEMP_MAX_C),
            ("Макс. температура GPU (°C):", self.temp_gpu_max, DEFAULT_GPU_TEMP_MAX, 85, Config.GPU_TEMP_MAX_C),
        ]:
            tk.Label(body_temp, text=label,
                     font=FONT_SMALL, bg=BG2, fg=TEXT_MED).pack(anchor="w", pady=(6, 0))
            tk.Scale(body_temp, variable=var, from_=60, to=max_allowed,
                     orient="horizontal", bg=BG2, fg=TEXT, troughcolor=BG3,
                     highlightthickness=0, bd=0, sliderlength=16,
                     activebackground=RED, font=FONT_SMALL).pack(fill="x")
            tk.Label(body_temp,
                     text=f"✅ Дефолт {default}°C  |  ⚠️ Выше {danger}°C — троттлинг",
                     font=FONT_SMALL, bg=BG2, fg=WARN).pack(anchor="w")

        # ── Инфо о безопасности ──
        body_safe = self._card(right, "Защита железа", "️")
        tk.Label(body_safe,
                 text="• CPU загружен максимум на 60-70% по умолчанию\n"
                      "• GPU VRAM лимит 60% — оставляет запас\n"
                      "• VRAM проверяется перед каждой загрузкой модели\n"
                      "• Если VRAM не хватает — автопереключение на CPU\n"
                      "• VRAM очищается после каждого видео\n"
                      "• Температурный предохранитель активен всегда\n"
                      "• Приоритет процесса понижен — система не тормозит",
                 font=FONT_SMALL, bg=BG2, fg=SUCCESS,
                 justify="left", wraplength=290).pack(anchor="w")

    # ── Нижняя панель (всегда видна) ──────────────────────────────────────────

    def _build_bottom(self, parent):
        bot = tk.Frame(parent, bg=BG, padx=20, pady=10)
        bot.pack(fill="x", side="bottom")
        # Фикс: при насыщенных вкладках нижняя зона не должна схлопываться.
        bot.configure(height=250)
        bot.pack_propagate(False)
        bot.columnconfigure(0, weight=1)

        btn_row = tk.Frame(bot, bg=BG); btn_row.pack(fill="x", pady=(0, 10))
        self.btn_run = tk.Button(btn_row, text="ЗАПУСТИТЬ НАРЕЗКУ",
                                 font=("Bahnschrift SemiBold", 13), bg=RED, fg=BG,
                                 bd=0, pady=10, activebackground=RED2, activeforeground=BG,
                                 highlightthickness=1, highlightbackground=BORDER, highlightcolor=RED2,
                                 cursor="hand2", relief="flat", command=self._start)
        self.btn_run.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=4)

        self.btn_pause = tk.Button(btn_row, text="Пауза",
                                   font=("Bahnschrift SemiBold", 11), bg=BG3, fg=TEXT_MED,
                                   bd=0, pady=10, activebackground=BG4,
                                   highlightthickness=1, highlightbackground=BORDER, highlightcolor=PURPLE2,
                                   cursor="hand2", relief="flat",
                                   command=self._toggle_pause, state="disabled", width=14)
        self.btn_pause.pack(side="left", ipady=4)

        prog = tk.Frame(bot, bg=BG2, padx=14, pady=10); prog.pack(fill="x", pady=(0, 8))
        top = tk.Frame(prog, bg=BG2); top.pack(fill="x", pady=(0, 4))
        tk.Label(top, text="EXPORT PROGRESS", font=FONT_SMALL, bg=BG2, fg=TEXT_DIM).pack(side="left")
        self.lbl_progress = tk.Label(top, text="", font=FONT_SMALL, bg=BG2, fg=TEXT_MED)
        self.lbl_progress.pack(side="right")
        style = ttk.Style(); style.theme_use("default")
        style.configure("X.Horizontal.TProgressbar",
                         troughcolor=BG3, background=RED2, thickness=10, bordercolor=BG2)
        self.progressbar = ttk.Progressbar(prog, variable=self.progress,
                                            maximum=100, style="X.Horizontal.TProgressbar")
        self.progressbar.pack(fill="x")

        lo = tk.Frame(bot, bg=BG2); lo.pack(fill="both", expand=True)
        lh = tk.Frame(lo, bg=BG3, padx=12, pady=5); lh.pack(fill="x")
        tk.Label(lh, text="SESSION LOG", font=FONT_LABEL, bg=BG3, fg=TEXT_DIM).pack(side="left")
        tk.Frame(lo, bg=BORDER, height=1).pack(fill="x")
        lb = tk.Frame(lo, bg=BG2); lb.pack(fill="both", expand=True)
        self.log = tk.Text(lb, bg=BG2, fg=TEXT, font=FONT_MONO, height=8, bd=0,
                           padx=14, pady=8, state="disabled", wrap="word",
                           insertbackground=PURPLE2)
        sb2 = tk.Scrollbar(lb, command=self.log.yview, bg=BG2, troughcolor=BG2, width=6)
        self.log.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y"); self.log.pack(fill="both", expand=True)
        self.log.tag_config("info",    foreground=TEXT_MED)
        self.log.tag_config("success", foreground=SUCCESS)
        self.log.tag_config("error",   foreground=DANGER)
        self.log.tag_config("accent",  foreground=PURPLE2)
        self.log.tag_config("warn",    foreground=WARN)

        self._log("✅ FFmpeg найден — готов к работе!", "success")
        if GPU.available:
            self._log(f" {GPU.name} | VRAM {GPU.vram_total}MB | CUDA {GPU.cuda_version}", "accent")
            self._log(f"️ Лимит CPU {self.cpu_threads.get()} потоков | GPU VRAM {self.gpu_vram_limit.get()}% | GPU порог {self.temp_gpu_max.get()}°C", "info")
        else:
            self._log("⚠️ GPU не найден — работаем на CPU", "warn")

    # ══════════════════════════════════════════════════════════════════════════
    # ДЕЙСТВИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def _add_video(self):
        paths = filedialog.askopenfilenames(
            title="Выбери видео",
            filetypes=[("Видео", "*.mp4 *.mov *.avi *.mkv *.webm"), ("Все", "*.*")])
        for p in paths:
            if p not in self.video_paths:
                self.video_paths.append(p)
                self.queue_listbox.insert("end", os.path.basename(p))
                self._log(f"➕ {os.path.basename(p)}", "accent")

    def _remove_video(self):
        sel = self.queue_listbox.curselection()
        if sel:
            idx = sel[0]; self.queue_listbox.delete(idx); self.video_paths.pop(idx)

    def _yt_in(self, e):
        if self.yt_url.get() == "https://youtube.com/watch?v=...":
            self.yt_entry.delete(0, "end"); self.yt_entry.configure(fg=TEXT)

    def _yt_out(self, e):
        if not self.yt_url.get().strip():
            self.yt_entry.insert(0, "https://youtube.com/watch?v=...")
            self.yt_entry.configure(fg=TEXT_DIM)

    def _download_yt(self):
        url = self.yt_url.get().strip()
        if not url or url == "https://youtube.com/watch?v=...":
            messagebox.showerror("Ошибка", "Вставь ссылку!"); return
        try: import yt_dlp
        except ImportError:
            messagebox.showerror("Ошибка", "pip install yt-dlp"); return
        self._log("⬇️ Скачиваю...", "accent")
        self.btn_run.configure(state="disabled")
        def do():
            try:
                od = os.path.join(os.path.expanduser("~"), "Desktop", "yt_downloads")
                os.makedirs(od, exist_ok=True)
                opts = {"format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
                        "outtmpl": os.path.join(od, "%(title)s.%(ext)s"),
                        "ffmpeg_location": os.path.dirname(FFMPEG), "quiet": True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    fn   = ydl.prepare_filename(info)
                self.video_paths.append(fn)
                self.after(0, lambda: self.queue_listbox.insert("end", os.path.basename(fn)))
                self._safe_log(f"✅ {os.path.basename(fn)}", "success")
            except Exception as ex:
                self._safe_log(f"❌ {ex}", "error")
            finally:
                self.after(0, lambda: self.btn_run.configure(state="normal"))
        threading.Thread(target=do, daemon=True).start()

    def _pick_folder(self):
        p = filedialog.askdirectory()
        if p: self.out_folder.set(p)

    def _toggle_pause(self) -> None:
        if not self.running: return
        if self._state == AppState.PAUSED:
            self._set_state(AppState.RUNNING)
            self.pause_event.set()
            self._worker.resume()
            self.btn_pause.configure(text="⏸ Пауза", fg=TEXT_MED)
            self._log("▶️ Продолжаем...", "accent")
            logger.info("Пауза снята")
        else:
            self._set_state(AppState.PAUSED)
            self.pause_event.clear()
            self._worker.pause()
            self.btn_pause.configure(text="▶ Продолжить", fg=SUCCESS)
            self._log("⏸ Пауза — нажми продолжить когда будешь готов", "warn")
            logger.info("Пауза установлена")

    def _start(self) -> None:
        if self.running: return
        if not self.video_paths:
            messagebox.showerror("Ошибка", "Добавь видео!"); return
        if self.mode.get() not in ("split",) and self.min_dur.get() >= self.max_dur.get():
            messagebox.showerror("Ошибка", "Мин.сек должен быть меньше Макс.сек!"); return

        self._sanitize_safety_settings(source="start")
        if str(self.fx_backend.get()).strip().lower() == "gpu" and not bool(self.use_gpu.get()):
            self._safe_log("⚠️ Зеркало/Зум/Цвет/9:16/Скорость будут на CPU, т.к. общий GPU выключен", "warn")
        self._safe_log(f"{self._runtime_pipeline_summary()}", "info")
        self._apply_cpu_limit()
        self._set_state(AppState.RUNNING)
        self.stop_flag.clear()
        self.pause_event.set()
        self._set_progress(0)
        self.btn_run.configure(state="disabled", bg=BG4, text="⏳   ОБРАБОТКА...")
        self.btn_pause.configure(state="normal")
        logger.info(f"Запуск нарезки: {len(self.video_paths)} видео, режим={self.mode.get()}")

        # Кладём всю очередь в worker — он сам обработает по порядку
        self._worker = VideoWorker(on_event=self._on_worker_event)
        self._worker.submit(self._process_queue)
        self._worker.start()

    # ══════════════════════════════════════════════════════════════════════════
    # ОБРАБОТКА ОЧЕРЕДИ
    # ══════════════════════════════════════════════════════════════════════════

    def _process_queue(self) -> None:
        total = len(self.video_paths); t0 = time.time()
        logger.info(f"Начинаю обработку очереди: {total} видео")
        try:
            for idx, vp in enumerate(self.video_paths):
                if self.stop_flag.is_set():
                    logger.info("Обработка прервана пользователем")
                    break
                self._safe_log(f"\n Видео {idx + 1}/{total}: {os.path.basename(vp)}", "accent")
                logger.info(f"Обрабатываю [{idx+1}/{total}]: {vp}")
                mode = self.mode.get()
                if mode == "split":
                    self._process_split(vp, t0)
                elif mode == "hybrid":
                    self._process_hybrid(vp, t0)
                else:
                    self._process_one(vp, t0)
                GPU.clear_vram()
                self._throttle()
        except Exception as e:
            logger.exception(f"Критическая ошибка в очереди: {e}")
            self._safe_log(f"❌ Критическая ошибка: {e}", "error")
        finally:
            elapsed = int(time.time() - t0)
            interrupted = self.stop_flag.is_set()
            if interrupted:
                self._safe_progress(0, f"Остановлено через {elapsed}с")
                self._safe_log(f"\n⏹ Остановлено пользователем через {elapsed}с", "warn")
                logger.info(f"Очередь остановлена пользователем через {elapsed}с")
            else:
                self._safe_progress(100, f"Готово за {elapsed}с")
                self._safe_log(f"\n✅ Готово! {total} видео за {elapsed}с", "success")
                logger.info(f"Очередь завершена за {elapsed}с")
            self._set_state(AppState.IDLE)
            if not interrupted:
                self.after(0, lambda: open_folder(self.out_folder.get()))
            self.after(0, lambda: self.btn_run.configure(
                state="normal", bg=RED, text="▶   ЗАПУСТИТЬ НАРЕЗКУ"))
            self.after(0, lambda: self.btn_pause.configure(state="disabled"))

    # ══════════════════════════════════════════════════════════════════════════
    # НАРЕЗКА ПО ВРЕМЕНИ
    # ══════════════════════════════════════════════════════════════════════════

    def _process_split(self, video_path: str, t0: float) -> None:
        """Нарезка по времени без AI — просто режет равными кусками."""
        try:
            out_folder = self.out_folder.get()
            clip_dur   = self.split_dur.get()
            duration   = self._get_duration(video_path)
            if duration == 0: raise RuntimeError("Не удалось определить длительность")
            self._safe_log(f"⏱️ {int(duration // 60)}:{int(duration % 60):02d}")

            starts = list(range(0, int(duration), clip_dur))
            if self.split_limit.get(): starts = starts[:self.num_clips.get()]

            os.makedirs(out_folder, exist_ok=True)
            base = os.path.splitext(os.path.basename(video_path))[0][:20]
            self._safe_log(f"✂️ Нарезаю {len(starts)} клипов по {clip_dur}с...", "accent")
            logger.info(f"Нарезка по времени: {len(starts)} клипов по {clip_dur}с")

            for i, s in enumerate(starts, 1):
                if self.stop_flag.is_set():
                    logger.info("Нарезка прервана пользователем")
                    break
                if not self._wait_if_paused(): break
                e   = min(s + clip_dur, duration)
                out = os.path.join(out_folder, f"{base}_part_{i:03d}.mp4")
                self._safe_progress(
                    int(i / len(starts) * 100),
                    f"Клип {i}/{len(starts)} | {int(time.time() - t0)}с")
                self._safe_log(
                    f"  [{i}/{len(starts)}] {int(s // 60):02d}:{int(s % 60):02d}"
                    f" → {int(e // 60):02d}:{int(e % 60):02d}")
                self._ffmpeg_cut(video_path, s, e - s, out)
        except Exception as e:
            logger.exception(f"Ошибка нарезки по времени: {e}")
            self._safe_log(f"❌ Ошибка: {e}", "error")

    # ══════════════════════════════════════════════════════════════════════════
    # ГИБРИД РЕЖИМ
    # ══════════════════════════════════════════════════════════════════════════

    def _process_hybrid(self, video_path, t0):
        """CPU + GPU работают параллельно в отдельных потоках."""
        tmp_audio = None
        try:
            duration = self._get_duration(video_path)
            if duration == 0: raise Exception("Не удалось определить длительность")
            self._safe_log(
                f"⏱️ {int(duration // 60)}:{int(duration % 60):02d} — Гибрид режим")

            self._safe_progress(5, "Извлекаю аудио...")
            tmp_audio = self._extract_audio(video_path)
            energy    = self._calc_energy(tmp_audio, duration)
            ns        = int(duration)

            cpu_res = {}; gpu_res = {}
            motion_gpu = self._is_motion_gpu_enabled()

            def cpu_worker():
                if not self._wait_if_paused(): return
                if self.stop_flag.is_set(): return
                if self.opt_motion.get() and not motion_gpu:
                    self._safe_log(" CPU: движение...", "info")
                    cpu_res["motion"] = self._analyze_motion(video_path, duration)
                    self._throttle()
                if self.stop_flag.is_set(): return
                if self.opt_laugh.get():
                    cpu_res["laugh"] = self._analyze_laugh(energy)
                cpu_res["audio"] = normalize(energy)
                self._safe_log("✅ CPU анализ завершён", "success")

            def gpu_worker():
                if not self._wait_if_paused(): return
                if self.stop_flag.is_set(): return
                if self.opt_motion.get() and motion_gpu:
                    self._safe_log(" GPU: движение...", "info")
                    gpu_res["motion"] = self._analyze_motion(video_path, duration)
                    self._check_and_wait_temp()
                    self._throttle()
                if not self._wait_if_paused(): return
                if self.stop_flag.is_set(): return
                if self.opt_whisper.get():
                    device = self._check_vram(500, "Whisper")
                    self._safe_log(f" Whisper ({device.upper()})...", "info")
                    gpu_res["speech"] = self._analyze_speech_faster(tmp_audio, duration, device)
                    self._check_and_wait_temp()
                    self._throttle()
                if not self._wait_if_paused(): return
                if self.stop_flag.is_set(): return
                if self.opt_faces.get():
                    device = self._check_vram(300, "Face Detector")
                    self._safe_log(f" Лица (GPU при возможности, {device.upper()})...", "info")
                    gpu_res["faces"] = self._analyze_faces(video_path, duration)
                    self._check_and_wait_temp()
                self._safe_log("✅ GPU анализ завершён", "success")

            self._safe_progress(15, "Гибрид анализ (CPU + GPU параллельно)...")
            t_cpu = threading.Thread(target=cpu_worker, daemon=True)
            t_gpu = threading.Thread(target=gpu_worker, daemon=True)
            t_cpu.start(); t_gpu.start()
            t_cpu.join();  t_gpu.join()

            # Фикс #2: после join проверяем emergency stop
            if self.stop_flag.is_set():
                logger.info("Гибрид: прерван после анализа (stop_flag)")
                return

            score = np.zeros(ns)
            score = safe_add(score, cpu_res.get("audio"),        self.w_audio.get())
            score = safe_add(score, cpu_res.get("laugh"),        self.w_laugh.get())
            score = safe_add(score, cpu_res.get("motion"),       self.w_motion.get())
            score = safe_add(score, gpu_res.get("motion"),       self.w_motion.get())
            score = safe_add(score, gpu_res.get("speech"),       self.w_speech.get())
            score = safe_add(score, gpu_res.get("faces"),        self.w_faces.get())

            GPU.clear_vram()
            self._cut_clips(score, video_path, duration, ns, t0)

        except Exception as ex:
            logger.exception(f"Гибрид ошибка: {ex}")
            self._safe_log(f"❌ Гибрид ошибка: {ex}", "error")
        finally:
            # Фикс #6: гарантированный cleanup
            if tmp_audio:
                try: os.remove(tmp_audio)
                except Exception as cleanup_err:
                    logger.debug(f"Не удалось удалить временный файл {tmp_audio}: {cleanup_err}")
            GPU.clear_vram()

    # ══════════════════════════════════════════════════════════════════════════
    # СТАНДАРТНЫЙ РЕЖИМ (fast / balanced / full)
    # ══════════════════════════════════════════════════════════════════════════

    def _check_stop_or_pause(self) -> bool:
        """
        Вспомогательный метод: проверяет паузу и emergency stop (фикс #1, #2).
        Возвращает True если нужно продолжить, False если нужно остановиться.
        """
        if self.stop_flag.is_set():
            return False
        return self._wait_if_paused()

    def _process_one(self, video_path, t0):
        tmp_audio = None
        try:
            mode     = self.mode.get()
            duration = self._get_duration(video_path)
            if duration == 0: raise Exception("Не удалось определить длительность")
            self._safe_log(f"⏱️ {int(duration // 60)}:{int(duration % 60):02d}")

            # Проверка до старта
            if not self._check_stop_or_pause(): return

            self._safe_progress(5, "Извлекаю аудио...")
            tmp_audio = self._extract_audio(video_path)

            if not self._check_stop_or_pause(): return
            energy = self._calc_energy(tmp_audio, duration)
            ns     = int(duration)
            score  = normalize(energy) * self.w_audio.get()

            if not self._check_stop_or_pause(): return
            if mode in ("balanced", "full") and self.opt_laugh.get():
                self._safe_progress(15, f"Смех... {int(time.time() - t0)}с")
                score = safe_add(score, self._analyze_laugh(energy), self.w_laugh.get())
                self._throttle()

            if not self._check_stop_or_pause(): return
            if mode in ("balanced", "full") and self.opt_motion.get():
                self._safe_progress(30, f"Движение... {int(time.time() - t0)}с")
                score = safe_add(score, self._analyze_motion(video_path, duration), self.w_motion.get())
                self._check_and_wait_temp()
                self._throttle()

            if not self._check_stop_or_pause(): return
            if mode == "full" and self.opt_whisper.get():
                self._safe_progress(45, f"Речь... {int(time.time() - t0)}с")
                score = safe_add(score, self._analyze_speech(tmp_audio, duration), self.w_speech.get())
                self._check_and_wait_temp()
                self._throttle()

            if not self._check_stop_or_pause(): return
            if mode == "full" and self.opt_faces.get():
                self._safe_progress(60, f"Лица... {int(time.time() - t0)}с")
                score = safe_add(score, self._analyze_faces(video_path, duration), self.w_faces.get())
                self._check_and_wait_temp()

            if not self._check_stop_or_pause(): return
            GPU.clear_vram()
            self._cut_clips(score, video_path, duration, ns, t0)

        except Exception as ex:
            logger.exception(f"Ошибка _process_one: {ex}")
            self._safe_log(f"❌ Ошибка: {ex}", "error")
        finally:
            # Гарантированный cleanup (фикс #6)
            if tmp_audio:
                try: os.remove(tmp_audio)
                except Exception as cleanup_err:
                    logger.debug(f"Не удалось удалить временный файл {tmp_audio}: {cleanup_err}")
            GPU.clear_vram()

    # ══════════════════════════════════════════════════════════════════════════
    # НАРЕЗКА КЛИПОВ
    # ══════════════════════════════════════════════════════════════════════════

    def _cut_clips(self, score: np.ndarray, video_path: str, duration: float, ns: int, t0: float) -> None:
        """Находит лучшие моменты по score и нарезает клипы."""
        num_clips  = self.num_clips.get()
        min_dur    = self.min_dur.get()
        max_dur    = self.max_dur.get()
        out_folder = self.out_folder.get()

        self._safe_progress(70, f"Ищу лучшие моменты... {int(time.time() - t0)}с")
        score = np.clip(score / max(np.max(score), 1), 0, 1)
        score = smooth(score, k=5)

        if np.max(score) == 0:
            logger.warning("Score пустой — нарезаю равномерно")
            cl = (min_dur + max_dur) // 2
            selected = [(s, s + cl) for s in range(0, ns - cl, max(cl, MIN_GAP + 1))][:num_clips]
        else:
            windows = []
            for s in range(ns):
                cl = int(np.random.randint(min_dur, max_dur + 1))
                e  = s + cl
                if e > ns: break
                seg = score[s:e]
                windows.append((s, e, float(np.mean(seg) * 0.5 + np.max(seg) * 0.5)))
            windows.sort(key=lambda x: x[2], reverse=True)
            selected = []
            for s, e, _ in windows:
                if not any(not (e + MIN_GAP <= ss or s - MIN_GAP >= ee) for ss, ee in selected):
                    selected.append((s, e))
                if len(selected) >= num_clips: break
            selected.sort()
            logger.info(f"Найдено {len(selected)} моментов из {num_clips} запрошенных")

        if not selected:
            self._safe_log("⚠️ Не найдено моментов.", "error")
            logger.warning("Не найдено ни одного момента для нарезки")
            return

        os.makedirs(out_folder, exist_ok=True)
        base = os.path.splitext(os.path.basename(video_path))[0][:20]
        self._safe_log(f"✂️ Нарезаю {len(selected)} клипов...", "accent")

        for i, (s, e) in enumerate(selected, 1):
            if self.stop_flag.is_set():
                logger.info("Нарезка прервана пользователем")
                break
            if not self._wait_if_paused(): break
            self._check_and_wait_temp()
            e   = min(e, duration)
            out = os.path.join(out_folder, f"{base}_clip_{i:02d}.mp4")
            self._safe_progress(
                70 + int(i / len(selected) * 28),
                f"Клип {i}/{len(selected)} | {int(time.time() - t0)}с")
            self._safe_log(
                f"  [{i}/{len(selected)}] "
                f"{int(s // 60):02d}:{int(s % 60):02d} → {int(e // 60):02d}:{int(e % 60):02d}")
            logger.info(f"Клип {i}: {s:.0f}с → {e:.0f}с → {out}")
            self._ffmpeg_cut(video_path, s, e - s, out)
            self._throttle()

    # ══════════════════════════════════════════════════════════════════════════
    # FFMPEG ХЕЛПЕРЫ
    # ══════════════════════════════════════════════════════════════════════════

    def _get_duration(self, video_path: str) -> float:
        """Получает длительность видео через ffprobe с таймаутом."""
        try:
            res = subprocess.run(
                [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", video_path],
                capture_output=True, text=True,
                creationflags=NO_WINDOW,
                timeout=Config.PROBE_TIMEOUT)
            info     = json.loads(res.stdout)
            duration = float(info["format"].get("duration", 0) or 0)
            logger.info(f"Длительность {os.path.basename(video_path)}: {duration:.1f}с")
            return duration
        except subprocess.TimeoutExpired:
            logger.error(f"ffprobe таймаут для {video_path}")
            return 0
        except Exception as e:
            logger.error(f"Ошибка получения длительности {video_path}: {e}")
            return 0

    def _extract_audio(self, video_path: str) -> str:
        """Извлекает аудио в WAV файл с retry. Бросает AudioError если не удалось."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_audio = tmp.name
        logger.info(f"Извлекаю аудио из {os.path.basename(video_path)}...")

        def _attempt():
            r = subprocess.run(
                [FFMPEG, "-y", "-threads", str(max(1, self.cpu_threads.get())),
                 "-i", video_path,
                 "-vn", "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1", tmp_audio],
                capture_output=True, creationflags=NO_WINDOW, timeout=1800)
            if r.returncode != 0:
                raise AudioError(f"FFmpeg код {r.returncode}: {r.stderr.decode(errors='ignore')[:80]}")
            return tmp_audio

        try:
            result = with_retry(_attempt, retries=1, delay=3.0,
                                error_cls=AudioError, context="extract_audio")
            logger.info(f"Аудио извлечено: {tmp_audio}")
            return result
        except Exception as e:
            try: os.remove(tmp_audio)
            except Exception as cleanup_err:
                logger.debug(f"Не удалось удалить временный файл {tmp_audio}: {cleanup_err}")
            raise AudioError(f"Не удалось извлечь аудио: {e}", recoverable=False)

    def _calc_energy(self, tmp_audio: str, duration: float) -> np.ndarray:
        """Вычисляет энергию аудио по секундам."""
        with wave.open(tmp_audio, 'rb') as wf:
            fpa = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if len(arr) > Config.AUDIO_MAX_SAMPLES:
                arr = arr[:Config.AUDIO_MAX_SAMPLES]
        ns = int(duration); energy = np.zeros(ns)
        for i in range(ns):
            chunk = arr[i * fpa:min(i * fpa + fpa, len(arr))]
            if len(chunk) > 0:
                energy[i] = float(np.sqrt(np.mean(chunk ** 2)))
        return energy

    def _build_vf(self):
        """
        Правильный порядок фильтров FFmpeg:
        зум → зеркало → цвет → вертикальный → скорость
        Субтитры вжигаются отдельно до зеркала.
        """
        f = []
        if self.opt_zoom.get():
            f.append("zoompan=z='1.02':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080")
        if self.opt_mirror.get():
            f.append("hflip")
        if self.opt_color.get():
            f.append("eq=contrast=1.05:brightness=0.03:saturation=1.2")
        if self.opt_vertical.get():
            f.append("crop=ih*9/16:ih,scale=1080:1920")

        speed = self.speed_val.get()
        if speed != "1x":
            sp = {"0.5x": 0.5, "1.5x": 1.5, "2x": 2.0}.get(speed, 1.0)
            f.append(f"setpts={1/sp:.4f}*PTS")

        return ",".join(f) if f else None

    def _effective_fx_backend(self) -> str:
        """Какой backend реально применяем для эффектов и скорости: cpu или gpu."""
        requested = str(self.fx_backend.get()).strip().lower()
        if requested not in ("cpu", "gpu"):
            requested = "gpu" if GPU.available else "cpu"
        if requested == "gpu" and (not GPU.available or not bool(self.use_gpu.get())):
            return "cpu"
        return requested

    def _detect_motion_gpu_capability(self, force: bool = False) -> bool:
        """
        Проверяет, может ли OpenCV CUDA реально выполнять анализ движения.
        Кешируем результат, чтобы не дёргать проверку на каждый redraw UI.
        """
        if self._motion_gpu_cap_checked and not force:
            return bool(self._motion_gpu_capable)

        capable = False
        try:
            import cv2
            cuda_count = 0
            if hasattr(cv2, "cuda") and hasattr(cv2.cuda, "getCudaEnabledDeviceCount"):
                try:
                    cuda_count = int(cv2.cuda.getCudaEnabledDeviceCount())
                except Exception:
                    cuda_count = 0
            capable = (
                cuda_count > 0 and
                hasattr(cv2, "cuda_GpuMat") and
                hasattr(cv2.cuda, "cvtColor") and
                hasattr(cv2.cuda, "resize") and
                hasattr(cv2.cuda, "absdiff")
            )
        except Exception as e:
            logger.debug(f"Motion GPU capability check failed: {e}")
            capable = False

        self._motion_gpu_capable = bool(capable)
        self._motion_gpu_cap_checked = True
        return bool(capable)

    def _is_motion_gpu_enabled(self) -> bool:
        """Должен ли анализ движения идти по GPU на текущих настройках."""
        if self._effective_fx_backend() != "gpu":
            return False
        return self._detect_motion_gpu_capability()

    def _analysis_backend_summary(self) -> str:
        """Кратко: где выполняется Этап 1 (поиск моментов) для выбранного режима."""
        mode = str(self.mode.get()).strip().lower()
        gpu_ai_available = bool(GPU.available) and bool(self.use_gpu.get())
        motion_gpu = bool(self.opt_motion.get()) and self._is_motion_gpu_enabled()
        gpu_ai_components_enabled = bool(self.opt_whisper.get() or self.opt_faces.get())
        if mode == "fast":
            return "1) Поиск моментов: CPU (режим Быстрый)"
        if mode == "balanced":
            if motion_gpu:
                return "1) Поиск моментов: CPU + GPU (режим Баланс, движение на GPU)"
            return "1) Поиск моментов: CPU (режим Баланс)"
        if mode == "hybrid":
            hybrid_gpu = motion_gpu or (gpu_ai_available and gpu_ai_components_enabled)
            if hybrid_gpu:
                return "1) Поиск моментов: CPU + GPU (режим Гибрид)"
            if gpu_ai_available and not gpu_ai_components_enabled and not motion_gpu:
                return "1) Поиск моментов: CPU (режим Гибрид, GPU-компоненты AI выключены)"
            return "1) Поиск моментов: CPU (режим Гибрид, GPU для AI недоступен/выключен)"
        if mode == "full":
            full_gpu = motion_gpu or (self.opt_faces.get() and gpu_ai_available)
            if full_gpu:
                return "1) Поиск моментов: CPU + GPU (режим Полный, часть компонентов на GPU)"
            return "1) Поиск моментов: CPU (режим Полный)"
        if mode == "split":
            return "1) Поиск моментов: выключен (режим По времени)"
        return "1) Поиск моментов: CPU"

    def _effects_backend_summary(self) -> str:
        """Кратко: где выполняется Этап 2 (рендер + эффекты) на текущих настройках."""
        requested = str(self.fx_backend.get()).strip().lower()
        effective = self._effective_fx_backend()
        if requested == "gpu" and effective == "cpu":
            return "2) Рендер + эффекты: CPU (GPU выбран, но общий GPU выключен/недоступен)"
        if effective == "gpu":
            return "2) Рендер + эффекты: GPU (NVENC)"
        return "2) Рендер + эффекты: CPU (libx264)"

    def _analysis_components_summary(self) -> str:
        """Список AI-компонентов, которые реально участвуют в анализе сейчас."""
        mode = str(self.mode.get()).strip().lower()
        gpu_ai_available = bool(GPU.available) and bool(self.use_gpu.get())
        if mode == "split":
            return "Компоненты анализа: не используются (режим По времени)"

        active = ["громкость"]
        disabled = []

        if mode in ("balanced", "full", "hybrid"):
            if self.opt_laugh.get():
                active.append("смех (CPU)")
            else:
                disabled.append("смех")
            if self.opt_motion.get():
                motion_where = "GPU" if self._is_motion_gpu_enabled() else "CPU"
                active.append(f"движение ({motion_where})")
            else:
                disabled.append("движение")

        if mode in ("full", "hybrid"):
            if self.opt_whisper.get():
                if mode == "hybrid":
                    active.append("речь (GPU/CPU авто)")
                else:
                    active.append("речь (CPU)")
            else:
                disabled.append("речь")
            if self.opt_faces.get():
                if gpu_ai_available:
                    active.append("лица (GPU/CPU авто)")
                else:
                    active.append("лица (CPU)")
            else:
                disabled.append("лица")

        text = f"Компоненты анализа: {', '.join(active)}"
        if disabled:
            text += f" | выключено: {', '.join(disabled)}"
        return text

    def _runtime_pipeline_details(self) -> tuple:
        """Подробная расшифровка текущего пайплайна."""
        return (
            self._analysis_backend_summary(),
            self._effects_backend_summary(),
            self._analysis_components_summary(),
        )

    def _runtime_pipeline_summary(self) -> str:
        """Единая строка для логов: на чем реально пойдут анализ и эффекты."""
        a, e, c = self._runtime_pipeline_details()
        return f"{a} | {e} | {c}"

    def _build_ffmpeg_cut_cmd(
        self,
        video_path: str,
        start: float,
        duration_sec: float,
        out_path: str,
        backend: str
    ) -> List[str]:
        """Собирает команду FFmpeg для нарезки клипа с выбранным CPU/GPU backend эффектов."""
        vf = self._build_vf()
        speed = self.speed_val.get()
        threads = str(max(1, self.cpu_threads.get()))

        cmd = [
            FFMPEG, "-y",
            "-threads", threads,      # общий лимит потоков FFmpeg
            "-ss", str(start),
            "-i", video_path,
            "-t", str(duration_sec)
        ]
        if vf:
            cmd += ["-vf", vf]

        af_parts = []
        if speed != "1x":
            sp = {"0.5x": 0.5, "1.5x": 1.5, "2x": 2.0}.get(speed, 1.0)
            if sp > 2.0:
                af_parts += ["atempo=2.0", f"atempo={sp/2.0:.2f}"]
            elif sp < 0.5:
                af_parts += ["atempo=0.5", f"atempo={sp*2.0:.2f}"]
            else:
                af_parts.append(f"atempo={sp:.2f}")
        if af_parts:
            cmd += ["-af", ",".join(af_parts)]

        if backend == "gpu":
            cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p5",
                "-c:a", "aac",
            ]
        else:
            cmd += [
                "-c:v", "libx264",
                "-threads", threads,   # отдельный лимит потоков x264
                "-c:a", "aac",
            ]

        cmd += [
            "-map", "0:v:0", "-map", "0:a:0?",
            "-avoid_negative_ts", "1", out_path
        ]
        return cmd

    def _run_ffmpeg(self, cmd: List[str], timeout: int = Config.FFMPEG_TIMEOUT,
                    retries: int = 1) -> Optional[subprocess.CompletedProcess]:
        """
        Запускает ffmpeg с таймаутом, логированием и retry.
        retries=1 означает: 1 попытка + 1 повтор = 2 попытки всего.
        """
        logger.debug(f"FFmpeg команда: {' '.join(cmd[:6])}...")

        def _attempt():
            result = subprocess.run(
                cmd, capture_output=True,
                creationflags=NO_WINDOW,
                timeout=timeout)
            if result.returncode != 0:
                err = result.stderr.decode(errors="ignore")[:200]
                logger.warning(f"FFmpeg код {result.returncode}: {err}")
                raise FFmpegError(f"FFmpeg вернул код {result.returncode}: {err[:80]}")
            return result

        try:
            return with_retry(_attempt, retries=retries, delay=2.0,
                              error_cls=FFmpegError, context="FFmpeg")
        except FFmpegError as e:
            self._safe_log(f"⚠️ {e}", "error")
            return None
        except subprocess.TimeoutExpired:
            msg = f"⚠️ FFmpeg таймаут ({timeout}с) — пропускаю"
            self._safe_log(msg, "error")
            logger.error(msg)
            return None
        except Exception as e:
            err = handle_error(e, "FFmpeg")
            self._safe_log(f"⚠️ FFmpeg: {err}", "error")
            return None

    def _ffmpeg_cut(self, video_path: str, start: float, duration_sec: float, out_path: str) -> None:
        """Нарезает один клип с применением всех эффектов и ограничением потоков."""
        backend = self._effective_fx_backend()
        requested = str(self.fx_backend.get()).strip().lower()
        cmd = self._build_ffmpeg_cut_cmd(video_path, start, duration_sec, out_path, backend)
        logger.info(f"FFmpeg clip backend: requested={requested or 'cpu'}, effective={backend}")
        r = self._run_ffmpeg(cmd, timeout=Config.CLIP_TIMEOUT)
        if r is None and backend == "gpu":
            self._safe_log("⚠️ GPU-обработка клипа не удалась — повторяю на CPU", "warn")
            logger.warning("FFmpeg GPU clip path failed; retrying CPU path")
            cmd_cpu = self._build_ffmpeg_cut_cmd(video_path, start, duration_sec, out_path, "cpu")
            r = self._run_ffmpeg(cmd_cpu, timeout=Config.CLIP_TIMEOUT)
        if r is None:
            self._safe_log("  ⚠️ FFmpeg: клип пропущен после ошибки/таймаута", "error")

    # ══════════════════════════════════════════════════════════════════════════
    # AI АНАЛИЗ
    # ══════════════════════════════════════════════════════════════════════════

    def _analyze_laugh(self, energy: np.ndarray) -> np.ndarray:
        """Детект смеха по резким пикам громкости."""
        scores = np.zeros(len(energy))
        if len(energy) < 3: return scores
        me = np.mean(energy)
        for i in range(1, len(energy) - 1):
            if energy[i] > me * Config.LAUGH_THRESHOLD:
                scores[max(0, i - 2):min(len(scores), i + 3)] += 2.0
        return scores

    def _analyze_motion(self, video_path: str, duration: float) -> np.ndarray:
        """Анализ движения: GPU при включенном переключателе, иначе CPU."""
        prefer_gpu = self._effective_fx_backend() == "gpu"
        if prefer_gpu:
            if self._detect_motion_gpu_capability():
                scores_gpu = self._analyze_motion_gpu(video_path, duration)
                if scores_gpu is not None:
                    return scores_gpu
                # Runtime ошибка GPU-пути: дальше в сессии не пытаемся снова, уходим на CPU.
                self._motion_gpu_capable = False
                self._motion_gpu_cap_checked = True
            if not self._warned_motion_gpu_runtime:
                self._warned_motion_gpu_runtime = True
                self._safe_log("⚠️ Движение: GPU недоступен — использую CPU", "warn")
        return self._analyze_motion_cpu(video_path, duration)

    def _analyze_motion_cpu(self, video_path: str, duration: float) -> np.ndarray:
        """CPU путь анализа движения через попарное сравнение кадров."""
        scores = np.zeros(int(duration))
        cap = None
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            if fps <= 1:
                fps = 25
            skip = max(Config.MOTION_SKIP_BASE, int(duration / Config.MOTION_ADAPT_DIV))
            if self.mode.get() == "balanced":
                skip = max(skip, int(skip * Config.BALANCED_MOTION_SKIP_MULT))
            prev, fi = None, 0
            _throttle_every = max(1, int(fps * 2))  # примерно каждые ~2 секунды видео
            logger.info(f"Анализ движения (CPU): skip={skip}, duration={duration:.0f}с")
            while True:
                if self.stop_flag.is_set():
                    logger.info("Анализ движения (CPU) прерван (stop_flag)")
                    break
                if not self._wait_if_paused():
                    logger.info("Анализ движения (CPU) прерван (pause/stop)")
                    break
                ret, frame = cap.read()
                if not ret:
                    break
                msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                sec = int(msec / 1000) if msec > 0 else int(fi / fps)
                gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
                if prev is not None and sec < len(scores):
                    scores[sec] += float(cv2.absdiff(prev, gray).mean()) / 10.0
                prev = gray
                fi += 1

                if fi % _throttle_every == 0:
                    self._check_and_wait_temp()
                    self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())

                for _ in range(skip - 1):
                    if self.stop_flag.is_set():
                        break
                    if not self._wait_if_paused():
                        break
                    r2, _ = cap.read()
                    fi += 1
                    if not r2:
                        break
            logger.info("Анализ движения (CPU) завершён")
            self._safe_log("🎬 Движение проанализировано (CPU)", "success")
        except Exception as e:
            logger.error(f"Ошибка анализа движения (CPU): {e}", exc_info=True)
            self._safe_log(f"⚠️ Движение (CPU): {e}", "error")
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
        return scores

    def _analyze_motion_gpu(self, video_path: str, duration: float) -> Optional[np.ndarray]:
        """GPU путь анализа движения через OpenCV CUDA. Возвращает None при fallback на CPU."""
        scores = np.zeros(int(duration))
        cap = None
        try:
            import cv2
            cap = cv2.VideoCapture(video_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            if fps <= 1:
                fps = 25
            skip = max(Config.MOTION_SKIP_BASE, int(duration / Config.MOTION_ADAPT_DIV))
            if self.mode.get() == "balanced":
                skip = max(skip, int(skip * Config.BALANCED_MOTION_SKIP_MULT))
            prev_gpu, fi = None, 0
            _throttle_every = max(1, int(fps * 2))  # примерно каждые ~2 секунды видео
            logger.info(f"Анализ движения (GPU): skip={skip}, duration={duration:.0f}с")

            while True:
                if self.stop_flag.is_set():
                    logger.info("Анализ движения (GPU) прерван (stop_flag)")
                    break
                if not self._wait_if_paused():
                    logger.info("Анализ движения (GPU) прерван (pause/stop)")
                    break
                ret, frame = cap.read()
                if not ret:
                    break
                msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                sec = int(msec / 1000) if msec > 0 else int(fi / fps)

                gpu_frame = cv2.cuda_GpuMat()
                gpu_frame.upload(frame)
                gray_gpu = cv2.cuda.cvtColor(gpu_frame, cv2.COLOR_BGR2GRAY)
                small_gpu = cv2.cuda.resize(gray_gpu, (160, 90))

                if prev_gpu is not None and sec < len(scores):
                    diff_gpu = cv2.cuda.absdiff(prev_gpu, small_gpu)
                    diff = diff_gpu.download()
                    scores[sec] += float(diff.mean()) / 10.0

                prev_gpu = small_gpu
                fi += 1

                if fi % _throttle_every == 0:
                    self._check_and_wait_temp()
                    self.control_gpu_load(max_gpu=self._gpu_load_limit_by_temp_setting())
                    self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())

                for _ in range(skip - 1):
                    if self.stop_flag.is_set():
                        break
                    if not self._wait_if_paused():
                        break
                    r2, _ = cap.read()
                    fi += 1
                    if not r2:
                        break

            logger.info("Анализ движения (GPU) завершён")
            self._safe_log("🎬 Движение проанализировано (GPU)", "success")
            return scores
        except Exception as e:
            logger.warning(f"GPU путь движения недоступен, fallback на CPU: {e}")
            return None
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass

    def _analyze_speech(self, audio_path: str, duration: float) -> np.ndarray:
        """Обычный Whisper на CPU — запасной вариант."""
        scores = np.zeros(int(duration))
        try:
            import whisper
            if not self._wait_if_paused():
                return scores
            if self.whisper_model is None:
                logger.info("Загружаю Whisper tiny (CPU)...")
                self._safe_log(" Загружаю Whisper tiny (CPU)...", "info")
                self.whisper_model = whisper.load_model("tiny")
            result = self.whisper_model.transcribe(audio_path, fp16=False)
            if self.stop_flag.is_set():
                return scores
            scores = self._parse_segments(result.get("segments", []), scores)
            self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())
            logger.info("Whisper (CPU) завершён")
            self._safe_log(" Речь проанализирована (CPU)", "success")
        except Exception as e:
            logger.error(f"Whisper CPU ошибка: {e}")
            self._safe_log(f"⚠️ Whisper CPU: {e}", "error")
        return scores

    def _analyze_speech_faster(self, audio_path: str, duration: float, device: str = "cpu") -> np.ndarray:
        """
        faster-whisper с retry и fallback на CPU.
        Модель загружается один раз и переиспользуется.
        """
        scores = np.zeros(int(duration))

        def _load_and_transcribe():
            from faster_whisper import WhisperModel
            if self.whisper_model is None:
                compute = "float16" if device == "cuda" else "int8"
                cpu_threads = max(1, int(self.cpu_threads.get()))
                logger.info(f"Загружаю faster-whisper tiny ({device}, {compute})...")
                self._safe_log(f" Загружаю faster-whisper ({device.upper()}, {compute})...", "info")
                self.whisper_model = WhisperModel(
                    "tiny", device=device, compute_type=compute,
                    cpu_threads=cpu_threads, num_workers=1
                )
            segs, _ = self.whisper_model.transcribe(audio_path, beam_size=1)
            return segs

        try:
            if not self._wait_if_paused():
                return scores
            segments_iter = with_retry(
                _load_and_transcribe, retries=1, delay=3.0,
                error_cls=AIError, context="faster-whisper"
            )
            kws = [k.strip().lower() for k in self.keywords.get().split(",") if k.strip()]
            for i, seg in enumerate(segments_iter):
                # Фикс #2: emergency stop внутри Whisper цикла
                if self.stop_flag.is_set():
                    logger.info("Whisper анализ прерван (stop_flag)")
                    break
                if not self._wait_if_paused():
                    logger.info("Whisper анализ прерван (pause/stop)")
                    break
                s0, s1 = int(seg.start), int(seg.end)
                txt    = seg.text.lower()
                w      = 1.0 / max(s1 - s0 + 1, 1)
                for s in range(s0, min(s1 + 1, len(scores))):
                    scores[s] += w
                    for kw in kws:
                        if kw in txt: scores[s] += 3.0 * w
                if i % 5 == 0:
                    self._check_and_wait_temp()
                    self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())
            logger.info(f"faster-whisper ({device}) завершён")
            self._safe_log(f" Речь проанализирована ({device.upper()})", "success")

        except ImportError:
            logger.warning("faster-whisper не найден, переключаюсь на обычный Whisper")
            self._safe_log("⚠️ faster-whisper не найден — переключаю на обычный Whisper", "warn")
            self._cleanup_whisper()
            return self._analyze_speech(audio_path, duration)

        except AIError as e:
            logger.error(f"faster-whisper ошибка после retry: {e}")
            self._safe_log(f"⚠️ faster-whisper не удался — пробую CPU", "warn")
            GPU.clear_vram()
            self._cleanup_whisper()
            return self._analyze_speech(audio_path, duration)

        except Exception as e:
            err = handle_error(e, "faster-whisper")
            self._safe_log(f"⚠️ Whisper: {err} — пробую CPU", "warn")
            GPU.clear_vram()
            self._cleanup_whisper()
            return self._analyze_speech(audio_path, duration)

        return scores

    def _parse_segments(self, segments: list, scores: np.ndarray) -> np.ndarray:
        """Парсит сегменты Whisper и заполняет массив оценок."""
        kws = [k.strip().lower() for k in self.keywords.get().split(",") if k.strip()]
        for i, seg in enumerate(segments):
            if self.stop_flag.is_set():
                break
            if not self._wait_if_paused():
                break
            s0, s1 = int(seg["start"]), int(seg["end"])
            txt    = seg["text"].lower()
            w      = 1.0 / max(s1 - s0 + 1, 1)
            for s in range(s0, min(s1 + 1, len(scores))):
                scores[s] += w
                for kw in kws:
                    if kw in txt: scores[s] += 3.0 * w
            if i % 10 == 0:
                self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())
        return scores

    def _analyze_faces(self, video_path: str, duration: float) -> np.ndarray:
        """Детект лиц: сначала OpenCV YuNet CUDA, затем MediaPipe Tasks, затем OpenCV Haar."""
        scores = np.zeros(int(duration))
        try:
            import cv2

            detector_kind = ""
            detector = None
            cascade = None
            mp = None

            # 1) Предпочтительно: OpenCV YuNet на CUDA (реальный GPU путь без новых библиотек)
            gpu_requested = bool(self.use_gpu.get()) and bool(GPU.available)
            if gpu_requested:
                try:
                    model_path = self._find_face_gpu_model_path()
                    if not model_path:
                        raise FileNotFoundError(
                            "Не найдена ONNX модель YuNet для GPU-детектора лиц "
                            "(ищу в корне, models/, assets/)."
                        )
                    cuda_count = 0
                    if hasattr(cv2, "cuda") and hasattr(cv2.cuda, "getCudaEnabledDeviceCount"):
                        try:
                            cuda_count = int(cv2.cuda.getCudaEnabledDeviceCount())
                        except Exception:
                            cuda_count = 0
                    if cuda_count <= 0:
                        raise RuntimeError("OpenCV не видит CUDA-устройство (getCudaEnabledDeviceCount=0)")
                    in_size = (Config.FACE_GPU_INPUT_SIZE, Config.FACE_GPU_INPUT_SIZE)
                    try:
                        detector = cv2.FaceDetectorYN_create(
                            model_path, "", in_size,
                            Config.FACE_GPU_SCORE_THRESHOLD,
                            Config.FACE_GPU_NMS_THRESHOLD,
                            Config.FACE_GPU_TOP_K,
                            cv2.dnn.DNN_BACKEND_CUDA,
                            cv2.dnn.DNN_TARGET_CUDA_FP16
                        )
                    except Exception:
                        detector = cv2.FaceDetectorYN_create(
                            model_path, "", in_size,
                            Config.FACE_GPU_SCORE_THRESHOLD,
                            Config.FACE_GPU_NMS_THRESHOLD,
                            Config.FACE_GPU_TOP_K,
                            cv2.dnn.DNN_BACKEND_CUDA,
                            cv2.dnn.DNN_TARGET_CUDA
                        )
                    detector_kind = "opencv-yunet-cuda"
                    logger.info(f"Анализ лиц: OpenCV YuNet CUDA model={os.path.basename(model_path)}")
                    self._safe_log("⚡ Лица: GPU детектор активен (OpenCV CUDA)", "success")
                except Exception as gpu_err:
                    logger.warning(f"Face GPU detector unavailable, fallback to CPU path: {gpu_err}")
                    if isinstance(gpu_err, FileNotFoundError):
                        if not self._warned_no_face_gpu_model:
                            self._warned_no_face_gpu_model = True
                            self._safe_log("⚠️ Нет ONNX модели YuNet для GPU — перехожу на CPU детектор лиц", "warn")
                    else:
                        if not self._warned_no_face_gpu_runtime:
                            self._warned_no_face_gpu_runtime = True
                            self._safe_log("⚠️ GPU детектор лиц недоступен — перехожу на CPU детектор", "warn")

            # 2) Если GPU путь недоступен — MediaPipe Tasks (CPU)
            if not detector_kind:
                try:
                    import mediapipe as mp
                    from mediapipe.tasks import python as mp_python
                    from mediapipe.tasks.python import vision as mp_vision

                    model_path = self._find_face_task_model_path()
                    if not model_path:
                        raise FileNotFoundError(
                            "Не найдена модель для FaceDetector (.tflite). "
                            "Положи файл в папку с приложением или в models/."
                        )

                    options = mp_vision.FaceDetectorOptions(
                        base_options=mp_python.BaseOptions(model_asset_path=model_path),
                        running_mode=mp_vision.RunningMode.IMAGE,
                        min_detection_confidence=0.5,
                    )
                    detector = mp_vision.FaceDetector.create_from_options(options)
                    detector_kind = "mediapipe-tasks"
                    logger.info(f"Анализ лиц: MediaPipe Tasks model={os.path.basename(model_path)}")
                except Exception as mp_err:
                    # 3) Последний fallback: OpenCV Haar (CPU)
                    detector_kind = "haar"
                    cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
                    cascade = cv2.CascadeClassifier(cascade_path)
                    if cascade.empty():
                        raise RuntimeError(
                            f"MediaPipe недоступен ({mp_err}) и Haar cascade не загрузился: {cascade_path}"
                        )
                    if not self._warned_no_face_task_model:
                        self._warned_no_face_task_model = True
                        self._safe_log("⚠️ MediaPipe Tasks недоступен — использую OpenCV (Haar) для лиц", "warn")
                    logger.warning(f"Faces fallback to OpenCV Haar: {mp_err}")

            cap = cv2.VideoCapture(video_path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            if fps <= 1:
                fps = 25
            skip = max(Config.FACES_SKIP_BASE, int(duration / Config.FACES_ADAPT_DIV))
            fi = 0
            _throttle_every = max(1, int(fps * 2))  # примерно каждые ~2 секунды видео
            logger.info(f"Анализ лиц: detector={detector_kind}, skip={skip}, duration={duration:.0f}с")
            while True:
                if self.stop_flag.is_set():
                    logger.info("Анализ лиц прерван (stop_flag)")
                    break
                if not self._wait_if_paused():
                    logger.info("Анализ лиц прерван (pause/stop)")
                    break
                ret, frame = cap.read()
                if not ret:
                    break
                msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                sec = int(msec / 1000) if msec > 0 else int(fi / fps)
                detections = 0

                if detector_kind == "opencv-yunet-cuda":
                    try:
                        h, w = frame.shape[:2]
                        detector.setInputSize((w, h))
                        _, faces = detector.detect(frame)
                        detections = len(faces) if faces is not None else 0
                    except Exception as gpu_detect_err:
                        logger.warning(f"GPU face detect runtime error, fallback to Haar: {gpu_detect_err}")
                        if not self._warned_no_face_gpu_runtime:
                            self._warned_no_face_gpu_runtime = True
                            self._safe_log("⚠️ Ошибка GPU-детектора лиц — переключаюсь на CPU (Haar)", "warn")
                        detector_kind = "haar"
                        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
                        cascade = cv2.CascadeClassifier(cascade_path)
                        if cascade.empty():
                            raise RuntimeError(f"Не удалось переключиться на Haar: {cascade_path}")
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        found = cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(40, 40))
                        detections = len(found)
                elif detector_kind == "mediapipe-tasks":
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                    res = detector.detect(mp_image)
                    detections = len(res.detections) if res and res.detections else 0
                else:
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    found = cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(40, 40))
                    detections = len(found)

                if sec < len(scores) and detections:
                    scores[sec] += float(detections)
                fi += 1

                # Runtime throttle — каждые N кадров
                if fi % _throttle_every == 0:
                    self._check_and_wait_temp()
                    if detector_kind == "opencv-yunet-cuda":
                        self.control_gpu_load(max_gpu=self._gpu_load_limit_by_temp_setting())
                    self.control_load(max_cpu=self._cpu_load_limit_by_temp_setting())

                for _ in range(skip - 1):
                    if self.stop_flag.is_set():
                        break
                    if not self._wait_if_paused():
                        break
                    r2, _ = cap.read()
                    fi += 1
                    if not r2:
                        break

            cap.release()
            if detector is not None and detector_kind == "mediapipe-tasks" and hasattr(detector, "close"):
                detector.close()

            if detector_kind == "opencv-yunet-cuda":
                self._safe_log("👤 Лица проанализированы (GPU CUDA)", "success")
            else:
                self._safe_log("👤 Лица проанализированы (CPU)", "success")
            logger.info("Анализ лиц завершён")
        except Exception as e:
            logger.error(f"Ошибка анализа лиц: {e}", exc_info=True)
            self._safe_log(f"⚠️ Лица: {e}", "error")
        return scores


# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    print(">>> Старт приложения", flush=True)
    try:
        print(">>> Создаю ClipAIApp...", flush=True)
        app = ClipAIApp()
        print(">>> ClipAIApp создан!", flush=True)
        app.update()
        app.deiconify()
        print(">>> Запускаю mainloop...", flush=True)
        app.mainloop()
    except Exception as e:
        import traceback
        print(f">>> ОШИБКА: {e}", flush=True)
        traceback.print_exc()
        if sys.stdin and sys.stdin.isatty():
            input("Нажми Enter для выхода...")


