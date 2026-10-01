FeatureScript 2945;
import(path : "onshape/std/common.fs", version : "2945.0");
import(path : "onshape/std/geometry.fs", version : "2945.0");

annotation { "Feature Type Name" : "Universal Project Box" }
export const universalProjectBox = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Length" }
        isLength(definition.length, { (millimeter) : [40, 100, 300] } as LengthBoundSpec);

        annotation { "Name" : "Width" }
        isLength(definition.width, { (millimeter) : [30, 65, 200] } as LengthBoundSpec);

        annotation { "Name" : "Height" }
        isLength(definition.height, { (millimeter) : [15, 35, 120] } as LengthBoundSpec);

        annotation { "Name" : "Wall thickness" }
        isLength(definition.wallThickness, { (millimeter) : [1.2, 2.4, 6] } as LengthBoundSpec);

        annotation { "Name" : "Screw boss diameter" }
        isLength(definition.bossDiameter, { (millimeter) : [4, 7.5, 15] } as LengthBoundSpec);

        annotation { "Name" : "Screw hole diameter" }
        isLength(definition.screwHoleDiameter, { (millimeter) : [1.5, 2.7, 5] } as LengthBoundSpec);

        annotation { "Name" : "Post inset" }
        isLength(definition.postInset, { (millimeter) : [6, 10, 30] } as LengthBoundSpec);

        annotation { "Name" : "Cable port width" }
        isLength(definition.cablePortWidth, { (millimeter) : [4, 12, 40] } as LengthBoundSpec);

        annotation { "Name" : "Cable port height" }
        isLength(definition.cablePortHeight, { (millimeter) : [3, 8, 25] } as LengthBoundSpec);
    }
    {
        const L = definition.length;
        const W = definition.width;
        const H = definition.height;
        const wall = definition.wallThickness;

        const bossRadius = definition.bossDiameter / 2;
        const screwHoleRadius = definition.screwHoleDiameter / 2;
        const inset = definition.postInset;

        const portW = definition.cablePortWidth;
        const portH = definition.cablePortHeight;

        // Outer body
        fCuboid(context, id + "outerBox", {
            "corner1" : vector(-L / 2, -W / 2, 0 * millimeter),
            "corner2" : vector( L / 2,  W / 2, H)
        });

        // Hollow cut body
        fCuboid(context, id + "innerCut", {
            "corner1" : vector(-L / 2 + wall, -W / 2 + wall, wall),
            "corner2" : vector( L / 2 - wall,  W / 2 - wall, H + 2 * millimeter)
        });

        opBoolean(context, id + "hollowBox", {
            "tools" : qCreatedBy(id + "innerCut", EntityType.BODY),
            "targets" : qCreatedBy(id + "outerBox", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION
        });

        // Boss locations
        const x1 = -L / 2 + inset;
        const x2 =  L / 2 - inset;
        const y1 = -W / 2 + inset;
        const y2 =  W / 2 - inset;

        const bossTop = H - 1 * millimeter;

        // Four screw bosses
        fCylinder(context, id + "boss1", {
            "bottomCenter" : vector(x1, y1, wall),
            "topCenter" : vector(x1, y1, bossTop),
            "radius" : bossRadius
        });

        fCylinder(context, id + "boss2", {
            "bottomCenter" : vector(x2, y1, wall),
            "topCenter" : vector(x2, y1, bossTop),
            "radius" : bossRadius
        });

        fCylinder(context, id + "boss3", {
            "bottomCenter" : vector(x2, y2, wall),
            "topCenter" : vector(x2, y2, bossTop),
            "radius" : bossRadius
        });

        fCylinder(context, id + "boss4", {
            "bottomCenter" : vector(x1, y2, wall),
            "topCenter" : vector(x1, y2, bossTop),
            "radius" : bossRadius
        });

        opBoolean(context, id + "joinBosses", {
            "tools" : qUnion([
                qCreatedBy(id + "boss1", EntityType.BODY),
                qCreatedBy(id + "boss2", EntityType.BODY),
                qCreatedBy(id + "boss3", EntityType.BODY),
                qCreatedBy(id + "boss4", EntityType.BODY)
            ]),
            "targets" : qCreatedBy(id + "outerBox", EntityType.BODY),
            "operationType" : BooleanOperationType.UNION
        });

        // Screw hole cutters
        fCylinder(context, id + "hole1", {
            "bottomCenter" : vector(x1, y1, wall - 1 * millimeter),
            "topCenter" : vector(x1, y1, H + 1 * millimeter),
            "radius" : screwHoleRadius
        });

        fCylinder(context, id + "hole2", {
            "bottomCenter" : vector(x2, y1, wall - 1 * millimeter),
            "topCenter" : vector(x2, y1, H + 1 * millimeter),
            "radius" : screwHoleRadius
        });

        fCylinder(context, id + "hole3", {
            "bottomCenter" : vector(x2, y2, wall - 1 * millimeter),
            "topCenter" : vector(x2, y2, H + 1 * millimeter),
            "radius" : screwHoleRadius
        });

        fCylinder(context, id + "hole4", {
            "bottomCenter" : vector(x1, y2, wall - 1 * millimeter),
            "topCenter" : vector(x1, y2, H + 1 * millimeter),
            "radius" : screwHoleRadius
        });

        opBoolean(context, id + "cutScrewHoles", {
            "tools" : qUnion([
                qCreatedBy(id + "hole1", EntityType.BODY),
                qCreatedBy(id + "hole2", EntityType.BODY),
                qCreatedBy(id + "hole3", EntityType.BODY),
                qCreatedBy(id + "hole4", EntityType.BODY)
            ]),
            "targets" : qCreatedBy(id + "outerBox", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION
        });

        // Cable port cutter on front wall
        fCuboid(context, id + "cablePortCut", {
            "corner1" : vector(-portW / 2, -W / 2 - 1 * millimeter, wall + 4 * millimeter),
            "corner2" : vector( portW / 2, -W / 2 + wall + 1 * millimeter, wall + 4 * millimeter + portH)
        });

        opBoolean(context, id + "cutCablePort", {
            "tools" : qCreatedBy(id + "cablePortCut", EntityType.BODY),
            "targets" : qCreatedBy(id + "outerBox", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION
        });
    });