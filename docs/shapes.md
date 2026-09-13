# Geometric Shapes for Pen Plotting

A reference of shape families that plot well — grouped by what generates them: compass/straightedge construction, named design traditions, or algorithm/math.

General principle: pen plotters favor shapes with few pen lifts (ideally one continuous path), lines that don't overlap excessively, and enough line density to read as a form.

## Sacred Geometry (circle-based construction)

Built by stepping a compass set to the circle's radius around its own circumference — each step marks a point exactly 60° apart.

- **Seed of Life** — six circles of equal radius arranged around a center circle, each centered on the previous circle's edge.
- **Flower of Life** — Seed of Life extended outward with more rings of same-radius circles centered on intersection points.
- **Fruit of Life** — 13 circles selected from the Flower of Life pattern.
- **Metatron's Cube** — straight lines connecting all 13 Fruit of Life circle centers; contains outlines of all five Platonic solids.
- **Vesica Piscis** — two overlapping circles, each centered on the other's edge; the base unit of the whole family.
- **Tree of Life (Kabbalah)** — 10 nodes connected by 22 paths in a fixed arrangement.
- **Sri Yantra** — nine interlocking triangles around a central point (straight lines, not circles).
- **Hexagon / Hexagram** — connecting the six Seed-of-Life points directly (hexagon) or every other point (hexagram/Star of David).

## Named Design Traditions

- **Bauhaus / De Stijl** — flat blocks of circles, triangles, squares on a grid (Kandinsky, Albers, Mondrian); composition-driven rather than dense line work.
- **Op Art** — repeated stripes, waves, or grids distorted for optical vibration (Bridget Riley, Victor Vasarely).
- **Islamic geometric patterns (girih)** — star-and-polygon tilings from a small set of interlocking tile shapes.
- **Celtic knotwork** — continuous interlacing bands with over-under weave.
- **Art Deco motifs** — sunbursts, stepped zigzags, chevrons.
- **Greek key / meander** — repeating right-angle border pattern.
- **Kolam / Rangoli** — South Indian dot-grid line art; a single continuous loop winds around a grid of dots — inherently plotter-friendly.
- **Pipe / Truchet weave with offset shading** — grid of quarter-circle "elbow" tiles forming continuous S-curves, each stroke drawn as several parallel offset copies in different colors to fake tube shading (one pen color per pass).

## Algorithmic / Mathematical (exact, programmatically calculable)

### Space-filling curves
Recursive subdivision rules that trace every cell of a grid in one continuous path.
- Hilbert curve, Peano curve, Moore curve, Gosper curve (flowsnake), Sierpiński curve, Z-order/Morton curve.
- **Image-modulated Hilbert curve** — walk the curve over an image's pixel grid, vary local density/looping by pixel brightness, producing a single-line curve that resolves into the image from a distance.

### L-systems (Lindenmayer systems)
Grammar-based rewrite rules generate the path.
- Koch curve / snowflake, dragon curve, Sierpiński arrowhead/triangle, fractal plants and trees.

### Iterated function systems (IFS) / fractals
- Sierpiński gasket and carpet, Apollonian gasket, Barnsley fern, Mandelbrot/Julia set boundaries.

### Parametric curves
Direct equations, no recursion needed.
- Rose curves (r = cos(kθ)), epicycloids/hypocycloids (spirograph curves), Lissajous figures, Archimedean/logarithmic/golden spirals, superformula curves (Gielis).

### Strange attractors
Iterate a nonlinear equation many times and plot the path.
- Lorenz, de Jong, Clifford, Ikeda, Hénon.

### Cellular automata
- Rule 30/90/110, Conway's Game of Life — rendered as line/tile art.

### Tiling algorithms
- Truchet tiles (random per-cell orientation), Wang tiles (edge-matching), Penrose tiling (substitution/deflation), Voronoi/Delaunay (from a point set).

### Number-theoretic curves
- Modular multiplication circles (connect point n to n×k mod N around a circle — produces cardioid/epicycloid patterns), Ulam spiral, prime spirals.

### Noise-driven fields
- Perlin/simplex noise flow fields; contour lines extracted via marching squares.

## Other Plotter-Friendly Basics

- Nested/concentric polygons (same shape repeated at shrinking scale with slight rotation).
- Isometric cube grids / rhombus tiling.
- Circle packing and concentric arcs.
- Superellipse/superformula blob shapes.
