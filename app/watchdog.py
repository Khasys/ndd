"""Boucle de l'appli bloquée : ce qui tournait à ce moment-là.

Le journal notait les blocages sans leur cause (v0.13.3 : dix blocages de 6 à 18 s en deux heures au NAS). Un fil à
part relève, pendant le blocage, la pile de la boucle (lignes de l'appli), avec les tâches planifiées et les requêtes
en cours ; les nettoyages mémoire de Python de plus d'une seconde sont notés à part. Si ce fil est lui-même retenu, le
verrou de Python (GIL) était pris : nettoyage mémoire ou calcul en C, pas une attente.
"""

import asyncio
import collections
import gc
import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("domain-watch")

LAG = 2.0      # secondes sans battement de la boucle : bloquée (aucune requête servie)
GC_SLOW = 1.0  # secondes : un nettoyage mémoire plus long est noté
FRAMES = 4     # lignes de l'appli gardées par relevé, la plus profonde d'abord


def _stack(frame) -> str:
    """Lignes de l'appli de la plus profonde à la plus haute, précédées de la ligne en cours si elle est ailleurs
    (bibliothèque, SQLite)."""
    mine, top = [], None
    while frame is not None:
        code = frame.f_code
        where = f"{Path(code.co_filename).name}:{frame.f_lineno} {code.co_name}"
        if top is None:
            top = where
        if "/app/" in code.co_filename and len(mine) < FRAMES:
            mine.append(where)
        frame = frame.f_back
    if top and (not mine or mine[0] != top):
        mine.insert(0, top)
    return " < ".join(mine)


class Watchdog:
    def __init__(self, lag: float = LAG, tick: float = 0.25, every: float = 0.5):
        self.lag, self.tick, self.every = lag, tick, every
        self.beat = time.monotonic()
        self.loop_thread: int | None = None
        self.jobs: dict[str, float] = {}       # tâche planifiée en cours -> début
        self.requests: dict[int, tuple[str, float]] = {}  # requête en cours -> (méthode chemin, début)
        self.samples: list[tuple[str, list[str], list[str]]] = []  # relevés du blocage en cours
        self.held = 0.0  # plus long retard du fil de surveillance pendant le blocage : verrou de Python pris
        self.slow_gc: collections.deque = collections.deque(maxlen=50)
        self._gc_started: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- Boucle de l'appli -----------------------------------------------------------------------

    async def run(self) -> None:
        """Battement de la boucle ; à la reprise après un blocage, une ligne au journal avec ce qui a été relevé."""
        self.loop_thread = threading.get_ident()
        while True:
            started = time.monotonic()
            self.beat = started
            await asyncio.sleep(self.tick)
            self.beat = time.monotonic()
            late = self.beat - started - self.tick
            if late > self.lag:
                await asyncio.sleep(self.every * 2)  # le fil de surveillance, retenu lui aussi, note d'abord son retard
                log.warning("%s", self.report(late))
            self.samples, self.held = [], 0.0
            while self.slow_gc:
                generation, spent, collected = self.slow_gc.popleft()
                log.warning("Nettoyage mémoire de Python (génération %s) : %.1f s, %s objets libérés", generation,
                            spent, collected)

    def report(self, late: float) -> str:
        parts = [f"Boucle de l'appli bloquée {late:.1f} s : aucune requête servie pendant ce temps"]
        if self.samples:
            stacks = collections.Counter(stack for stack, _, _ in self.samples)
            stack, seen = stacks.most_common(1)[0]
            parts.append(f"dans {stack} ({seen} relevé{'s' if seen > 1 else ''} sur {len(self.samples)})")
            _, jobs, requests = self.samples[0]
        else:
            jobs, requests = self._now()
        if jobs:
            parts.append("tâches en cours : " + ", ".join(jobs))
        if requests:
            parts.append("requêtes en cours : " + ", ".join(requests))
        if self.held > self.lag:
            parts.append(f"fil de surveillance retenu {self.held:.1f} s (verrou de Python pris : nettoyage mémoire ou "
                         "calcul en C)")
        elif not self.samples:
            parts.append("aucun relevé")
        return ". ".join(parts)

    def _now(self) -> tuple[list[str], list[str]]:
        now = time.monotonic()
        jobs = [f"{name} depuis {now - at:.0f} s" for name, at in dict(self.jobs).items()]
        requests = [f"{what} depuis {now - at:.0f} s" for what, at in dict(self.requests).values()][:5]
        return jobs, requests

    # --- Tâches et requêtes en cours ---------------------------------------------------------------

    def job_started(self, name: str) -> None:
        self.jobs[name] = time.monotonic()

    def job_done(self, name: str) -> None:
        self.jobs.pop(name, None)

    # --- Fil de surveillance et nettoyages mémoire -------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop = threading.Event()  # un par fil : un ancien fil pas encore réveillé s'arrête quand même
        gc.callbacks.append(self._on_gc)
        self._thread = threading.Thread(target=self._watch, args=(self._stop,), name="watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)
        self._thread = None

    def _watch(self, stop: threading.Event) -> None:
        last = time.monotonic()
        while not stop.wait(self.every):
            now = time.monotonic()
            if now - last - self.every > self.lag:
                self.held = max(self.held, now - last - self.every)
            if now - self.beat > self.lag and self.loop_thread is not None:
                frame = sys._current_frames().get(self.loop_thread)
                if frame is not None:
                    self.samples.append((_stack(frame), *self._now()))
                    del self.samples[:-40]
            last = now

    def _on_gc(self, phase: str, info: dict) -> None:
        # Rien de lourd ici (ni journal ni verrou) : appelé autour de chaque nettoyage, dans n'importe quel fil.
        if phase == "start":
            self._gc_started = time.monotonic()
        elif self._gc_started is not None:
            spent = time.monotonic() - self._gc_started
            self._gc_started = None
            if spent > GC_SLOW:
                self.slow_gc.append((info.get("generation"), spent, info.get("collected")))
