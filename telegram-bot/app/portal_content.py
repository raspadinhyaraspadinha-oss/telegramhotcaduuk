from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class VideoItem:
    id: str
    title: str
    thumbnail: str
    min_age: int
    max_age: int
    people: List[str]


DEFAULT_MIN_AGE = 15
DEFAULT_MAX_AGE = 19

# Pessoas famosas para filtro (preencha como quiser)
FAMOUS_PEOPLE = [
    "Larissa Manoela",
    "Juliano Floss",
    "Camila Rocha",
    "Duda Martins",
    "Elisa Prado",
    "Fernanda Alves",
]

# Thumbnails (pode trocar por URLs/arquivos). Placeholder neutro:
DEFAULT_THUMBNAIL = "https://images.unsplash.com/photo-1524504388940-b1c1722653e1?q=80&w=600&auto=format&fit=crop"
# Thumbnail local para v3 (servida em /portal/media/thumb1.jpeg)
THUMB1_URL = "/portal/media/thumb1.jpeg"
THUMB2_URL = "/portal/media/follo1.jpg"

# Lista inicial de videos (preencha como quiser)
VIDEOS: List[VideoItem] = [
    VideoItem(
        id="v1",
        title="Ensaio Premium 01",
        thumbnail=DEFAULT_THUMBNAIL,
        min_age=15,
        max_age=17,
        people=["Ana Clara"],
    ),
    VideoItem(
        id="v2",
        title="Coleção Noturna 02",
        thumbnail=THUMB2_URL,
        min_age=15,
        max_age=18,
        people=["Suruba da escola de Mariana - MG"],
    ),
    VideoItem(
        id="v3",
        title="Especial Vip 03",
        thumbnail=THUMB1_URL,
        min_age=15,
        max_age=17,
        people=["Larissa Manoela"],
    ),
    VideoItem(
        id="v4",
        title="Pack Exclusivo 04",
        thumbnail=DEFAULT_THUMBNAIL,
        min_age=18,
        max_age=19,
        people=["Elisa Prado", "Fernanda Alves"],
    ),
]
