from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from config import CHARGE_COEFF, BASE_V0


@dataclass(frozen=True)
class ProjectileProfile:
    name: str
    table_folder: str


@dataclass(frozen=True)
class WeaponProfile:
    name: str
    kind: str
    projectiles: List[ProjectileProfile]


DEFAULT_WEAPON_CATALOG: Dict[str, WeaponProfile] = {
    "mortar_82": WeaponProfile(
        name="82мм миномет",
        kind="Миномет",
        projectiles=[
            ProjectileProfile(name="ОФ-832", table_folder="tables"),
            ProjectileProfile(name="Дым", table_folder="tables"),
        ],
    ),
    "howitzer_122": WeaponProfile(
        name="122мм гаубица",
        kind="Орудие",
        projectiles=[
            ProjectileProfile(name="ОФ-462", table_folder="tables"),
            ProjectileProfile(name="Актив-реактивный", table_folder="tables"),
        ],
    ),
    "grenade_launcher": WeaponProfile(
        name="Автоматический гранатомет",
        kind="Гранатомет",
        projectiles=[
            ProjectileProfile(name="ВОГ", table_folder="tables"),
        ],
    ),
}


class Weapon:
    def __init__(self):
        self.base_speed = float(BASE_V0)
        self.charges = dict(CHARGE_COEFF)

    def v0_for_charge(self, charge: int) -> float:
        return self.base_speed * float(self.charges[int(charge)])
