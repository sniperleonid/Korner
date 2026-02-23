from config import CHARGE_COEFF, BASE_V0

class Weapon:
    def __init__(self):
        self.base_speed = float(BASE_V0)
        self.charges = dict(CHARGE_COEFF)

    def v0_for_charge(self, charge: int) -> float:
        return self.base_speed * float(self.charges[int(charge)])
