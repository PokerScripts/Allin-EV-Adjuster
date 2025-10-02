#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ev.py — расчет all-in adjusted EV из Hand History (PokerStars формат).
"""

import argparse
import csv
import datetime as dt
import itertools
import logging
import math
import os
import random
import re
import sys
from pathlib import Path
from typing import List, Tuple, Dict, Optional

try:
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


# -----------------------------------------------------------
# Утилиты
# -----------------------------------------------------------

def parse_args():
    """Парсим аргументы командной строки."""
    ap = argparse.ArgumentParser(description="All-in adjusted EV calculator for PokerStars HH")
    ap.add_argument("--hh", required=True, nargs="+", help="Путь к файлу или папке с HH")
    ap.add_argument("--out", help="CSV файл для экспорта результатов")
    ap.add_argument("--plot", help="PNG файл для графика Net vs EV")
    ap.add_argument("--from", dest="date_from", help="Дата с (YYYY-MM-DD)")
    ap.add_argument("--to", dest="date_to", help="Дата по (YYYY-MM-DD)")
    ap.add_argument("--stakes", help="Фильтр по лимиту, через запятую")
    ap.add_argument("--mode", choices=["cash", "mtt", "auto"], default="auto")
    ap.add_argument("--mc-iters", type=int, default=50000, help="Число итераций Монте-Карло")
    ap.add_argument("--seed", type=int, help="Seed для генератора случайных чисел")
    ap.add_argument("--ev-before-rake", action="store_true", help="Считать EV до рейка")
    ap.add_argument("--assume-random-opponent", action="store_true", help="Если карты оппа не показаны, добрать случайно")
    ap.add_argument("--jobs", type=int, default=1, help="Количество параллельных процессов (пока не реализовано)")
    ap.add_argument("--verbose", action="store_true")
    return ap.parse_args()


def setup_logging(verbose=False):
    """Настройка логирования."""
    lvl = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )


# -----------------------------------------------------------
# Модели данных
# -----------------------------------------------------------

class Player:
    def __init__(self, name, cards=None):
        self.name = name
        self.cards = cards or []  # карманные карты


class Hand:
    """Простая структура для хранения информации по раздаче."""
    def __init__(self, hand_id, date, hero, players, board, rake, total_pot, results):
        self.hand_id = hand_id
        self.date = date
        self.hero = hero
        self.players = players  # {name: Player}
        self.board = board      # список открытых карт (строки 'Ah', 'Ks' и т.п.)
        self.rake = rake
        self.total_pot = total_pot
        self.results = results  # {name: float} кто сколько выиграл/проиграл


# -----------------------------------------------------------
# Парсер HH (упрощенный для PokerStars)
# -----------------------------------------------------------

def parse_hh_file(path: Path) -> List[Hand]:
    hands = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    blocks = re.split(r"PokerStars Hand #", text)
    for blk in blocks[1:]:
        try:
            hand_id = blk.split()[0]
            date = dt.datetime.now()
            hero = "Hero"
            players = {hero: Player(hero)}
            board = []
            rake = 0.0
            total_pot = 0.0
            results = {}
            hands.append(Hand(hand_id, date, hero, players, board, rake, total_pot, results))
        except Exception as e:
            logging.warning(f"Ошибка парсинга раздачи: {e}")
            continue
    return hands


def read_hands(paths: List[str]) -> List[Hand]:
    all_hands = []
    for p in paths:
        path = Path(p)
        if path.is_file():
            all_hands.extend(parse_hh_file(path))
        elif path.is_dir():
            for f in path.glob("*.txt"):
                all_hands.extend(parse_hh_file(f))
    return all_hands


# -----------------------------------------------------------
# Оценка комбинаций (упрощенная)
# -----------------------------------------------------------

RANKS = "23456789TJQKA"
SUITS = "shdc"

def deck() -> List[str]:
    return [r + s for r in RANKS for s in SUITS]


def hand_strength(cards: List[str]) -> int:
    # Очень грубое: сравним только старшую карту
    ranks = [RANKS.index(c[0]) for c in cards]
    return max(ranks)


# -----------------------------------------------------------
# Monte Carlo оценка equity
# -----------------------------------------------------------

def monte_carlo_equity(hero_cards, villain_cards, board, mc_iters=10000, seed=None) -> float:
    if seed:
        random.seed(seed)

    dead = set(hero_cards + villain_cards + board)
    deck_left = [c for c in deck() if c not in dead]

    hero_wins = 0
    ties = 0

    for _ in range(mc_iters):
        # добираем доску до 5 карт
        need = 5 - len(board)
        sample = random.sample(deck_left, need)
        full_board = board + sample

        hero_best = hand_strength(hero_cards + full_board)
        villain_best = hand_strength(villain_cards + full_board)

        if hero_best > villain_best:
            hero_wins += 1
        elif hero_best == villain_best:
            ties += 1

    return (hero_wins + ties * 0.5) / mc_iters


# -----------------------------------------------------------
# Основной расчет EV
# -----------------------------------------------------------

def process_hand(hand: Hand, args) -> Dict:
    """
    Обрабатываем одну раздачу.
    Возвращает словарь с полями для отчета.
    Сейчас — сильно упрощено: считаем equity только против одного оппа.
    """
    hero = hand.players.get(hand.hero)
    # Для примера считаем против первого другого игрока
    villain = None
    for name, pl in hand.players.items():
        if name != hand.hero:
            villain = pl
            break

    if not hero or not villain or not hero.cards or not villain.cards:
        return None

    equity = monte_carlo_equity(hero.cards, villain.cards, hand.board, args.mc_iters, args.seed)
    eligible_pot = hand.total_pot - (0 if args.ev_before_rake else hand.rake)
    hero_invested = eligible_pot / 2  # очень грубое приближение!
    ev_contrib = equity * eligible_pot - hero_invested
    hero_net = hand.results.get(hand.hero, 0.0)

    return {
        "hand_id": hand.hand_id,
        "date": hand.date,
        "hero": hand.hero,
        "equity": equity,
        "eligible_pot": eligible_pot,
        "hero_invested": hero_invested,
        "ev_contrib": ev_contrib,
        "hero_net": hero_net
    }


# -----------------------------------------------------------
# Отчеты
# -----------------------------------------------------------

def write_csv(rows: List[Dict], out_path: str):
    """Сохраняем результаты в CSV."""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def plot_graph(rows: List[Dict], out_path: str):
    """Рисуем график Net vs EV."""
    if not HAS_MPL or not rows:
        logging.warning("matplotlib не установлен или нет данных")
        return

    cum_net, cum_ev = [], []
    total_net, total_ev = 0, 0
    for r in rows:
        total_net += r["hero_net"]
        total_ev += r["ev_contrib"]
        cum_net.append(total_net)
        cum_ev.append(total_ev)

    plt.figure(figsize=(8, 5))
    plt.plot(cum_net, label="Net")
    plt.plot(cum_ev, label="EV")
    plt.xlabel("Hands")
    plt.ylabel("Chips / $")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(out_path)
    logging.info(f"График сохранен в {out_path}")


# -----------------------------------------------------------
# main
# -----------------------------------------------------------

def main():
    args = parse_args()
    setup_logging(args.verbose)

    hands = read_hands(args.hh)
    logging.info(f"Прочитано раздач: {len(hands)}")

    rows = []
    for h in hands:
        r = process_hand(h, args)
        if r:
            rows.append(r)

    if not rows:
        logging.info("Нет данных для отчета")
        return

    # Сводка
    net_total = sum(r["hero_net"] for r in rows)
    ev_total = sum(r["ev_contrib"] for r in rows)
    logging.info(f"Net total: {net_total:.2f}, EV total: {ev_total:.2f}, Diff: {ev_total - net_total:.2f}")

    # CSV
    if args.out:
        write_csv(rows, args.out)
        logging.info(f"CSV сохранен в {args.out}")

    # График
    if args.plot:
        plot_graph(rows, args.plot)


if __name__ == "__main__":
    main()