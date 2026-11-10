class _T:
    def __init__(self, v): self.value = v
class _Txt:
    def __init__(self, v): self.text = _T(v)
class Field:
    def __init__(self, name, v): self.name = name; self.text = _T(v)
class FootprintInstance:
    pass
class Via:
    def __init__(self): self.position = None; self.net = None; self.diameter = 0; self.drill_diameter = 0
class Track:
    def __init__(self): self.start = None; self.end = None; self.width = 0; self.layer = None; self.net = None
