"""QR codes with the standard library alone, for connecting a phone.

Byte mode at error correction level M, versions 1 to 10, which holds any address
this app shows. Follows ISO/IEC 18004. It's here rather than a library because
this project has no dependencies by design.

    encode(text)        -> rows of True (dark) / False (light)
    terminal(matrix)    -> text for a console, two modules to a character
    svg(matrix)         -> an image for the page
"""
from __future__ import annotations

Matrix = list[list[bool]]

# version: (error correction codewords per block, [(blocks, data codewords each), ...])
LEVEL_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
             7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
LEVEL_M_BITS = 0b00
MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: r * c % 2 + r * c % 3 == 0,
    lambda r, c: (r * c % 2 + r * c % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + r * c % 3) % 2 == 0,
]


# ------------------------------------------------------- error correction ----
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
_EXP[255:510] = _EXP[:255]


def _mul(a: int, b: int) -> int:
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        grown = poly + [0]
        for j, term in enumerate(poly):
            grown[j + 1] ^= _mul(term, _EXP[i])
        poly = grown
    return poly


def _correction(data: list[int], degree: int) -> list[int]:
    """The remainder of dividing the data by the generator polynomial."""
    generator = _generator(degree)
    rest = data + [0] * degree
    for i in range(len(data)):
        factor = rest[i]
        if factor:
            for j, term in enumerate(generator):
                rest[i + j] ^= _mul(term, factor)
    return rest[len(data):]


# ------------------------------------------------------------- the message ----
def _capacity(version: int) -> int:
    return sum(count * size for count, size in LEVEL_M[version][1])


def _count_bits(version: int) -> int:
    return 8 if version < 10 else 16


def _fit(data: bytes) -> int:
    for version in LEVEL_M:
        if 4 + _count_bits(version) + len(data) * 8 <= _capacity(version) * 8:
            return version
    raise ValueError(f"{len(data)} bytes is too long for a QR code here")


def _codewords(data: bytes, version: int) -> list[int]:
    """Data split into blocks, each with its correction, interleaved."""
    room = _capacity(version) * 8
    bits = f"0100{len(data):0{_count_bits(version)}b}" + "".join(f"{b:08b}" for b in data)
    bits += "0" * min(4, room - len(bits))
    bits += "0" * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(words) < _capacity(version):
        words.append((0xEC, 0x11)[pad % 2])
        pad += 1

    degree, groups = LEVEL_M[version]
    blocks, at = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[at:at + size])
            at += size
    corrections = [_correction(block, degree) for block in blocks]

    out: list[int] = []
    for i in range(max(len(block) for block in blocks)):
        out += [block[i] for block in blocks if i < len(block)]
    for i in range(degree):
        out += [correction[i] for correction in corrections]
    return out


# ---------------------------------------------------------------- the grid ----
class _Grid:
    def __init__(self, version: int):
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def put(self, row: int, col: int, dark: bool) -> None:
        self.dark[row][col], self.fixed[row][col] = dark, True

    def finder(self, top: int, left: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                row, col = top + r, left + c
                if 0 <= row < self.size and 0 <= col < self.size:
                    edge = r in (0, 6) and 0 <= c <= 6 or c in (0, 6) and 0 <= r <= 6
                    self.put(row, col, edge or (2 <= r <= 4 and 2 <= c <= 4))

    def alignment(self, row: int, col: int) -> None:
        for r in range(-2, 3):
            for c in range(-2, 3):
                self.put(row + r, col + c, max(abs(r), abs(c)) != 1)

    def format(self, mask: int) -> None:
        data = LEVEL_M_BITS << 3 | mask
        rest = data
        for _ in range(10):
            rest = rest << 1 ^ (rest >> 9) * 0x537
        bits = (data << 10 | rest) ^ 0x5412
        bit = [bool(bits >> i & 1) for i in range(15)]
        for i in range(6):
            self.put(i, 8, bit[i])
        self.put(7, 8, bit[6])
        self.put(8, 8, bit[7])
        self.put(8, 7, bit[8])
        for i in range(9, 15):
            self.put(8, 14 - i, bit[i])
        for i in range(8):
            self.put(8, self.size - 1 - i, bit[i])
        for i in range(8, 15):
            self.put(self.size - 15 + i, 8, bit[i])
        self.put(self.size - 8, 8, True)

    def version_info(self) -> None:
        rest = self.version
        for _ in range(12):
            rest = rest << 1 ^ (rest >> 11) * 0x1F25
        bits = self.version << 12 | rest
        for i in range(18):
            dark = bool(bits >> i & 1)
            a, b = self.size - 11 + i % 3, i // 3
            self.put(b, a, dark)
            self.put(a, b, dark)

    def patterns(self) -> None:
        last = self.size - 7
        self.finder(0, 0)
        self.finder(0, last)
        self.finder(last, 0)
        for i in range(self.size):
            if not self.fixed[6][i]:
                self.put(6, i, i % 2 == 0)
            if not self.fixed[i][6]:
                self.put(i, 6, i % 2 == 0)
        centres = ALIGNMENT[self.version]
        end = len(centres) - 1
        for i, row in enumerate(centres):
            for j, col in enumerate(centres):
                if (i, j) not in ((0, 0), (0, end), (end, 0)):      # finders sit there
                    self.alignment(row, col)
        self.format(0)                  # reserves its cells; the real one comes last
        if self.version >= 7:
            self.version_info()

    def place(self, words: list[int]) -> None:
        """Lay the bits out in two-wide columns, snaking up and down from the
        bottom right. Leftover cells stay light."""
        bits = [bool(w >> i & 1) for w in words for i in range(7, -1, -1)]
        at, upward, col = 0, True, self.size - 1
        while col > 0:
            if col == 6:
                col -= 1
            for row in (range(self.size - 1, -1, -1) if upward else range(self.size)):
                for c in (col, col - 1):
                    if not self.fixed[row][c] and at < len(bits):
                        self.dark[row][c] = bits[at]
                        at += 1
            upward = not upward
            col -= 2

    def masked(self, mask: int) -> Matrix:
        copy = _Grid(self.version)
        copy.fixed = [row[:] for row in self.fixed]
        rule = MASKS[mask]
        copy.dark = [[dark != (not self.fixed[r][c] and rule(r, c))
                      for c, dark in enumerate(row)] for r, row in enumerate(self.dark)]
        copy.format(mask)
        return copy.dark


def _penalty(grid: Matrix) -> int:
    """How hard this pattern is to read: long runs, blocks, finder look-alikes,
    and too much of one colour. The lowest score is the mask to use."""
    score, size = 0, len(grid)
    for line in grid + [list(column) for column in zip(*grid)]:
        run = 1
        for a, b in zip(line, line[1:]):
            if a == b:
                run += 1
                continue
            score += run - 2 if run >= 5 else 0
            run = 1
        score += run - 2 if run >= 5 else 0
        text = "".join("1" if dark else "0" for dark in line)
        score += 40 * (text.count("10111010000") + text.count("00001011101"))
    for r in range(size - 1):
        for c in range(size - 1):
            if grid[r][c] == grid[r][c + 1] == grid[r + 1][c] == grid[r + 1][c + 1]:
                score += 3
    share = sum(map(sum, grid)) * 100 // (size * size)
    return score + abs(share - 50) // 5 * 10


def encode(text: str) -> Matrix:
    data = text.encode("utf-8")
    version = _fit(data)
    grid = _Grid(version)
    grid.patterns()
    grid.place(_codewords(data, version))
    return min((grid.masked(mask) for mask in range(8)), key=_penalty)


# --------------------------------------------------------------- drawing ----
def _bordered(matrix: Matrix, quiet: int) -> Matrix:
    width = len(matrix) + quiet * 2
    blank = [False] * width
    side = [False] * quiet
    return [blank] * quiet + [side + row + side for row in matrix] + [blank] * quiet


def terminal(matrix: Matrix, indent: str = "  ", quiet: int = 4) -> str:
    """Half-block characters, so modules come out square. The colours are set
    explicitly: dark on white is what scanners expect, whatever the console's
    own colours are."""
    grid = _bordered(matrix, quiet)
    if len(grid) % 2:
        grid = grid + [[False] * len(grid[0])]
    lines = []
    for top, bottom in zip(grid[::2], grid[1::2]):
        cells, style = [], None
        for upper, lower in zip(top, bottom):
            wanted = f"\x1b[{30 if upper else 97};{40 if lower else 107}m"
            cells.append(("" if wanted == style else wanted) + "▀")
            style = wanted
        lines.append(indent + "".join(cells) + "\x1b[0m")
    return "\n".join(lines)


def svg(matrix: Matrix, quiet: int = 4) -> str:
    size = len(matrix) + quiet * 2
    cells = "".join(f"M{c + quiet} {r + quiet}h1v1h-1z" for r, row in enumerate(matrix)
                    for c, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
            f'<rect width="{size}" height="{size}" fill="#fff"/>'
            f'<path fill="#000" d="{cells}"/></svg>')
