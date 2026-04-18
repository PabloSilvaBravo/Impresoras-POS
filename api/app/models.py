from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class TextElement(BaseModel):
    type: Literal["text"]
    x_mm: float
    y_mm: float
    content: str
    font: str = "3"
    rotation: Literal[0, 90, 180, 270] = 0
    x_scale: int = Field(1, ge=1, le=10)
    y_scale: int = Field(1, ge=1, le=10)


class BarcodeElement(BaseModel):
    type: Literal["barcode"]
    x_mm: float
    y_mm: float
    content: str
    symbology: Literal["128", "128M", "39", "93", "EAN13", "EAN8", "UPCA", "UPCE", "I25", "CODA"] = "128"
    height_mm: float = 10.0
    human_readable: Literal[0, 1, 2, 3] = 2
    rotation: Literal[0, 90, 180, 270] = 0
    narrow: int = 2
    wide: int = 2


class QRCodeElement(BaseModel):
    type: Literal["qrcode"]
    x_mm: float
    y_mm: float
    content: str
    ecc: Literal["L", "M", "Q", "H"] = "M"
    cell_width: int = Field(5, ge=1, le=10)
    rotation: Literal[0, 90, 180, 270] = 0


class BoxElement(BaseModel):
    type: Literal["box"]
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    thickness: int = 2


class LineElement(BaseModel):
    type: Literal["line"]
    x_mm: float
    y_mm: float
    width_mm: float
    thickness_mm: float = 0.5


Element = Annotated[
    Union[TextElement, BarcodeElement, QRCodeElement, BoxElement, LineElement],
    Field(discriminator="type"),
]


class LabelSpec(BaseModel):
    width_mm: float | None = None
    height_mm: float | None = None
    gap_mm: float | None = None
    density: int | None = Field(None, ge=0, le=15)
    speed: int | None = Field(None, ge=1, le=6)
    direction: Literal[0, 1] = 1
    copies: int = Field(1, ge=1, le=999)
    elements: list[Element]


class RawPayload(BaseModel):
    data: str


class PrinterPublic(BaseModel):
    id: str
    name: str
    model: str
    device: str
    available: bool
