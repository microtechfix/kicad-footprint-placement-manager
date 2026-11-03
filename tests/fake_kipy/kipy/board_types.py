class _T:
    def __init__(self, v): self.value = v
class _Txt:
    def __init__(self, v): self.text = _T(v)
class Field:
    def __init__(self, name, v): self.name = name; self.text = _T(v)
class FootprintInstance:
    pass
