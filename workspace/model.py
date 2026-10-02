from build123d import *

with BuildPart() as plate:
    Box(60, 40, 8)
    fillet(plate.edges().filter_by(Axis.Z), radius=5)
    with Locations(plate.faces().sort_by(Axis.Z)[-1]):
        with GridLocations(40, 20, 2, 2):
            Hole(3)

result = plate.part
