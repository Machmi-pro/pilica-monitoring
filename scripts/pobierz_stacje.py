#!/usr/bin/env python3
"""
pobierz_stacje.py

Pobiera aktualny stan wody dla stacji hydrologicznych na rzece Pilica
z nieoficjalnego API hydro-back.imgw.pl (tego samego, którego używa
strona hydro.imgw.pl), liczy wskaźniki procentowe i zapisuje:

  - data/aktualny_stan.json        -> nadpisywany snapshot wszystkich stacji
  - data/historia/{id}.json        -> historia (append) per stacja

Uruchamiane cyklicznie (np. co 4h) przez GitHub Actions.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Konfiguracja ---------------------------------------------------------

STACJE = {
    "150190280": {"nazwa": "Wąsosz", "wojewodztwo": "śląskie"},
    "151190090": {"nazwa": "Przedbórz", "wojewodztwo": "łódzkie"},
    "151190100": {"nazwa": "Sulejów-Kopalnia", "wojewodztwo": "łódzkie"},
    "151200020": {"nazwa": "Spała", "wojewodztwo": "łódzkie"},
    "151200090": {"nazwa": "Nowe Miasto", "wojewodztwo": "mazowieckie"},
}

API_URL = "https://hydro-back.imgw.pl/station/hydro/status"

# hydro-back bywa wybredne co do requestów bez nagłówków (403),
# więc udajemy normalną przeglądarkę.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://hydro.imgw.pl/",
    "Accept": "application/json",
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORIA_DIR = DATA_DIR / "historia"
SNAPSHOT_PLIK = DATA_DIR / "aktualny_stan.json"

TIMEOUT = 15  # sekundy


# --- Pobieranie i przeliczanie --------------------------------------------

def pobierz_stacje(stacja_id: str) -> dict:
    """Pobiera surowe dane jednej stacji z hydro-back.imgw.pl."""
    resp = requests.get(
        API_URL, params={"id": stacja_id}, headers=HEADERS, timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def wylicz_wskazniki(poziom_cm, min_cm, max_cm, ostrzegawczy_cm):
    """Liczy procent_zakresu i procent_do_alarmowego. Zwraca (float|None, float|None)."""
    procent_zakresu = None
    if min_cm is not None and max_cm is not None and max_cm != min_cm:
        procent_zakresu = round(
            (poziom_cm - min_cm) / (max_cm - min_cm) * 100, 1
        )

    procent_do_alarmowego = None
    if ostrzegawczy_cm:  # None lub 0 -> pomijamy
        procent_do_alarmowego = round(poziom_cm / ostrzegawczy_cm * 100, 1)

    return procent_zakresu, procent_do_alarmowego


def przetworz(stacja_id: str, surowe: dict) -> dict:
    """Wyciąga potrzebne pola z odpowiedzi API i dolicza wskaźniki."""
    status = surowe.get("status", {})
    properties = surowe.get("properties", {})

    current = status.get("currentState") or {}
    previous = status.get("previousState") or {}

    poziom_cm = current.get("value")
    poprzedni_cm = previous.get("value")
    data_pomiaru = current.get("date")

    min_cm = properties.get("minimumStateValue")
    max_cm = properties.get("maximumStateValue")
    ostrzegawczy_cm = status.get("warningValue")
    alarmowy_cm = status.get("alarmValue")
    trend = status.get("trend")

    procent_zakresu, procent_do_alarmowego = wylicz_wskazniki(
        poziom_cm, min_cm, max_cm, ostrzegawczy_cm
    )

    info = STACJE[stacja_id]

    return {
        "nazwa": info["nazwa"],
        "rzeka": status.get("river", "Pilica"),
        "wojewodztwo": status.get("province", info["wojewodztwo"]),
        "poziom_cm": poziom_cm,
        "poprzedni_cm": poprzedni_cm,
        "data_pomiaru": data_pomiaru,
        "trend": trend,
        "min_cm": min_cm,
        "max_cm": max_cm,
        "ostrzegawczy_cm": ostrzegawczy_cm,
        "alarmowy_cm": alarmowy_cm,
        "procent_zakresu": procent_zakresu,
        "procent_do_alarmowego": procent_do_alarmowego,
    }


# --- Zapis do plików -------------------------------------------------------

def zapisz_snapshot(dane_stacji: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "aktualizacja": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stacje": dane_stacji,
    }
    SNAPSHOT_PLIK.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def dopisz_historie(stacja_id: str, dane: dict) -> None:
    HISTORIA_DIR.mkdir(parents=True, exist_ok=True)
    plik = HISTORIA_DIR / f"{stacja_id}.json"

    if plik.exists():
        try:
            historia = json.loads(plik.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            historia = []
    else:
        historia = []

    wpis = {
        "data_pomiaru": dane["data_pomiaru"],
        "poziom_cm": dane["poziom_cm"],
        "trend": dane["trend"],
        "procent_zakresu": dane["procent_zakresu"],
        "procent_do_alarmowego": dane["procent_do_alarmowego"],
    }

    # Nie dopisuj duplikatu, jeśli IMGW jeszcze nie zaktualizowało pomiaru
    # (np. cron trafił między odświeżeniami źródła co 2h).
    if historia and historia[-1].get("data_pomiaru") == wpis["data_pomiaru"]:
        return

    historia.append(wpis)
    plik.write_text(
        json.dumps(historia, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --- Main ------------------------------------------------------------------

def main() -> int:
    dane_stacji = {}
    bledy = []

    for stacja_id in STACJE:
        try:
            surowe = pobierz_stacje(stacja_id)
            dane = przetworz(stacja_id, surowe)
            dane_stacji[stacja_id] = dane
            dopisz_historie(stacja_id, dane)
            print(f"OK  {stacja_id} ({dane['nazwa']}): {dane['poziom_cm']} cm")
        except Exception as exc:  # noqa: BLE001 - chcemy złapać wszystko i iść dalej
            bledy.append((stacja_id, str(exc)))
            print(f"BŁĄD {stacja_id}: {exc}", file=sys.stderr)

    if dane_stacji:
        zapisz_snapshot(dane_stacji)

    if bledy:
        print(f"\nZakończono z błędami dla {len(bledy)} stacji.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
