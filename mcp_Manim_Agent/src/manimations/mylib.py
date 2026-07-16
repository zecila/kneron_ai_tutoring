from manim import *
import math
import numpy as np

def play_natural_number_sum_proof(scene: Scene):
    """
    Plays the visual proof of 1+2+...+n=n(n+1)/2. 
    Begin derivation with 2(1 + 2 + \dots + n) = A = b \cdot h = n(n+1)
    """
    square = Square(0.5, stroke_width=2, color=BLUE, fill_color=BLUE, fill_opacity=0.2).move_to([2, -1.25, 0])
    scene.play(Create(square))
    
    squares: list[Square] = []
    for i in range(1, 7):
        for j in range(i + 1):
            squares.append(Square(0.5, stroke_width=2, color=BLUE, fill_color=BLUE, fill_opacity=0.2).move_to([2 + 0.5 * i, -1.25 + 0.5 * j, 0]))
    
    scene.play(LaggedStart(*[Create(square) for square in squares], run_time=2, lag_ratio=0.05))
    
    group = VGroup(square, *squares).copy()
    scene.add(group)
    scene.play(group.animate.set_color(RED).set_fill(RED))
    scene.play(Rotate(group, angle=PI, about_point=[3.25, 0.25, 0]))
    
    rect = Rectangle(height=3.5, width=4, fill_opacity=0, stroke_width=0).move_to([3.25, 0.25, 0])
    
    label1 = BraceLabel(rect, r"n", LEFT)
    label2 = BraceLabel(rect, r"n+1", DOWN)
    scene.play(Create(label1))
    scene.play(Create(label2))

def play_circ_area_proof(scene: Scene):
    """
    Plays the visual proof of the area of a circle. 
    Begin derivation with A_{circ} = A_{rect}
    """
    circle = Circle(radius=1.25)
    num_wedges = 32
    wedges: list[AnnularSector] = []
    for i in range(num_wedges):
        angle = i * 2 * PI / num_wedges
        wedges.append(AnnularSector(
            inner_radius=0,
            outer_radius=circle.radius,
            start_angle=angle,
            angle=2 * PI / num_wedges,
            color=BLUE,
            fill_opacity=0.2,
            stroke_width=1
        ))
    scene.play(Create(circle))
    scene.play(*[Create(wedge) for wedge in wedges])
    wedge2 = AnnularSector(
        inner_radius=0,
        outer_radius=2,
        start_angle=3 * PI / 2 - PI / num_wedges,
        angle=2 * PI / num_wedges,
        color=BLUE,
        fill_opacity=0.2,
        stroke_width=1
    )
    wedges2: list[AnnularSector] = []
    for i in range(num_wedges // 2):
        wedges2.append(AnnularSector(
            inner_radius=0,
            outer_radius=circle.radius,
            start_angle=3 * PI / 2 - PI / num_wedges,
            angle=2 * PI / num_wedges,
            color=BLUE,
            fill_opacity=0.2,
            stroke_width=1
        ).move_to([2.5 + i * 2 * circle.radius * math.sin(PI / num_wedges), 0, 0]))
    
    for i in range(num_wedges // 2):
        wedges2.append(AnnularSector(
            inner_radius=0,
            outer_radius=circle.radius,
            start_angle=PI / 2 - PI / num_wedges,
            angle=2 * PI / num_wedges,
            color=BLUE,
            fill_opacity=0.2,
            stroke_width=1
        ).move_to([2.5 + ((i * 2 - 1) * circle.radius) * math.sin(PI / num_wedges), circle.radius * (1 - math.cos(PI / num_wedges)), 0]))
    
    scene.play(LaggedStart(*[Transform(wedges[i], wedges2[i]) for i in range(num_wedges)], lag_ratio = 0.1, run_time=4))
    
    rect = Rectangle(height=circle.radius, width=PI * circle.radius, fill_opacity=0, stroke_width=0).move_to([2.5 + PI * circle.radius / 2, 0, 0])
    label1 = BraceLabel(rect, r"h=r", LEFT)
    label2 = BraceLabel(rect, r"b=\frac{c}{2}=\pi r", DOWN)
    scene.play(Create(label1))
    scene.play(Create(label2))

def play_deriv(*lines, scene: Scene):
    """
    Plays derivations.
    """
    texs = []
    for i, line in enumerate(lines):
        if i == 0:
            texs.append(MathTex(line, font_size=48).to_corner(UL))
        else:
            texs.append(MathTex(line, font_size=48).next_to(texs[i-1], DOWN, buff=0.2, aligned_edge=LEFT))
        scene.play(Write(texs[i]))

def play_pythag_visual(scene: Scene, a=None, b=None, c=None):
    """
    Plays Pythagorean theorem visualization, with a square on each side of the right triangle. 
    Start derivation with a^2 + b^2 = c^2
    """
    oa = a if a != None else "?"
    ob = b if b != None else "?"
    oc = c if c != None else "?"
    if a != None and b != None and c != None:
        pass
    elif a == None and b != None and c != None:
        a = round(math.sqrt(c ** 2 - b ** 2), 2)
    elif a != None and b == None and c != None:
        b = round(math.sqrt(c ** 2 - a ** 2), 2)
    elif a != None and b != None and c == None:
        c = round(math.sqrt(a ** 2 + b ** 2))
    else:
        a = 3
        b = 4
        c = 5
    ratio = 3 / c
    C = [2, 0, 0]
    B = [2 + a * ratio, 0, 0]
    A = [2, b * ratio, 0]
    triangle = Polygon(A, B, C)
    scene.play(Create(triangle))
    a_label = LabeledLine(start=B, end=C, label=MathTex(f"a={oa}", font_size=24))
    b_label = LabeledLine(start=A, end=C, label=MathTex(f"b={ob}", font_size=24))
    c_label = LabeledLine(start=B, end=A, label=MathTex(f"c={oc}", font_size=24))
    scene.play(Create(a_label))
    scene.play(Create(b_label))
    scene.play(Create(c_label))
    for label, square in zip(['a', 'c', 'b'], squares_on_edges(A, B, C)):
        scene.play(Create(square))
        scene.play(Write(MathTex(label + "^2").move_to(square.get_center())))
        
def play_pythag_proof(scene: Scene):
    """
    Plays Pythagorean theorem proof, with four triangles and two squares.
    Begin derivation with A_{big square} = A_{four triangles} + A_{small square}.
    A_{big square} = (a+b)^2, A_{small square} = c^2, A_{four triangles} = 4 \cdot \frac{1}{2}ab
    """
    A = np.array([0, -3.5, 0])
    B = np.array([0, 3.5, 0])
    C = np.array([7, 3.5, 0])
    D = np.array([7, -3.5, 0])
    a = 4
    W = A + np.array([0, a, 0])
    X = B + np.array([a, 0, 0])
    Y = C + np.array([0, -a, 0])
    Z = D + np.array([-a, 0, 0])
    
    square = Square(side_length = 7).move_to([3.5, 0, 0])
    inner = square_in_square(square, 5)
    scene.play(Create(square))
    scene.play(Create(inner))
    a_labels = [(A, W), (B, X), (C, Y), (D, Z)]
    b_labels = [(W, B), (X, C), (Y, D), (Z, A)]
    c_labels = [(W, X), (X, Y), (Y, Z), (Z, W)]
    scene.play(*[Create(LabeledLine(start=start, end=end, label=MathTex("a", font_size=24), color=RED)) for start, end in a_labels])
    scene.play(*[Create(LabeledLine(start=start, end=end, label=MathTex("b", font_size=24), color=GREEN)) for start, end in b_labels])
    scene.play(*[Create(LabeledLine(start=start, end=end, label=MathTex("c", font_size=24), color=BLUE)) for start, end in c_labels])

def squares_on_edges(*points, **kwargs) -> list[Polygon]:
    """
    Creates squares on the edges defined by the given points.
    Args:
        *points: A variable number of points defining the vertices of the polygon.
        **kwargs: Additional keyword arguments to pass to the Polygon constructor.
    
    Returns:
        list[Polygon]: A list of squares created on the edges of the polygon defined by the points.
    """
    if not is_ccw(points):
        points = points[::-1]
        
    with open("debug.txt", "w") as f:
        f.write(f"Points: {points}\n")
    
    squares = []
    for i in range(len(points)):
        p1 = points[i]
        p2 = points[(i + 1) % len(points)]
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        p3 = [p2[0] + dy, p2[1] - dx, p2[2]]
        p4 = [p1[0] + dy, p1[1] - dx, p1[2]]
        square = Polygon(p1, p2, p3, p4, **kwargs)
        squares.append(square)
    return squares

def square_in_square(outer_square: Polygon, inner_len: float, **kwargs):
    """
    Creates a square inside another square.

    Args:
        outer_square (Polygon): The outer square.
        inner_len (float): The side length of the inner square.
        **kwargs: Additional keyword arguments to pass to the Polygon constructor.

    Returns:
        Square: The inner square.
    """
    center = outer_square.get_center()
    outer_points = outer_square.get_vertices()
    outer_len = np.linalg.norm(outer_points[0] - outer_points[1])
    if inner_len < outer_len / math.sqrt(2):
        inner_len = outer_len / math.sqrt(2) + 0.1
    y = 1/2 * (outer_len - math.sqrt(2 * inner_len**2 - outer_len**2))
    theta = math.asin(y / inner_len)
    return Square(side_length=inner_len, **kwargs).move_to(center).rotate(theta)

def is_ccw(points) -> bool:
    """
    Returns True if the points are in counterclockwise (CCW) order, False if clockwise (CW).
    Uses the signed area method.
    """
    area = 0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i][:2]
        x2, y2 = points[(i + 1) % n][:2]
        area += (x2 - x1) * (y2 + y1)
    return area < 0