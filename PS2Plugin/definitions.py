from dataclasses import dataclass, field

@dataclass
class PS2Game:
    id: str
    name: str
    path: str
    metadata: dict = field(default_factory=dict)
