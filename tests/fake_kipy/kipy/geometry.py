class Vector2:
    def __init__(self, x=0, y=0): self.x, self.y = int(x), int(y)
    @classmethod
    def from_xy(cls, x, y): return cls(x, y)
    def __repr__(self): return f"V({self.x},{self.y})"
class Angle:
    def __init__(self, d=0.0): self.degrees = d
    @classmethod
    def from_degrees(cls, d): return cls(d)
