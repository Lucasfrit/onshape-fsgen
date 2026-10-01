FeatureScript 2945;
import(path : "onshape/std/geometry.fs", version : "2945.0");

const LARGE_LENGTH_BOUNDS =
{
    (millimeter) : [1, 100, 500]
} as LengthBoundSpec;

const SMALL_LENGTH_BOUNDS =
{
    (millimeter) : [0.1, 5, 100]
} as LengthBoundSpec;

const COUNT_BOUNDS =
{
    (unitless) : [1, 8, 100]
} as IntegerBoundSpec;


annotation { "Feature Type Name" : "Parametric Hybrid Wheel" }
export const parametricHybridWheel = defineFeature(function(context is Context, id is Id, definition is map)
    precondition
    {
        annotation { "Name" : "Overall diameter" }
        isLength(definition.outerDiameter, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Wheel width" }
        isLength(definition.wheelWidth, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Hub width" }
        isLength(definition.hubWidth, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Tread depth" }
        isLength(definition.treadDepth, SMALL_LENGTH_BOUNDS);

        annotation { "Name" : "Tread count" }
        isInteger(definition.treadCount, COUNT_BOUNDS);

        annotation { "Name" : "Rim diameter" }
        isLength(definition.rimDiameter, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Hub diameter" }
        isLength(definition.hubDiameter, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Axle hole diameter" }
        isLength(definition.axleHoleDiameter, SMALL_LENGTH_BOUNDS);

        annotation { "Name" : "Outer hole count" }
        isInteger(definition.outerHoleCount, COUNT_BOUNDS);

        annotation { "Name" : "Outer hole pitch diameter" }
        isLength(definition.outerHolePitchDiameter, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Outer hole diameter" }
        isLength(definition.outerHoleDiameter, SMALL_LENGTH_BOUNDS);

        annotation { "Name" : "Mount hole count" }
        isInteger(definition.mountHoleCount, COUNT_BOUNDS);

        annotation { "Name" : "Mount hole pitch diameter" }
        isLength(definition.mountHolePitchDiameter, LARGE_LENGTH_BOUNDS);

        annotation { "Name" : "Mount hole diameter" }
        isLength(definition.mountHoleDiameter, SMALL_LENGTH_BOUNDS);
    }
    {
        const zero = 0 * millimeter;

        const outerRadius = definition.outerDiameter / 2;
        const tireCoreRadius = outerRadius - definition.treadDepth;
        const halfWidth = definition.wheelWidth / 2;
        const hubHalfWidth = definition.hubWidth / 2;

        // Main tire cylinder
        fCylinder(context, id + "tireCore", {
            "bottomCenter" : vector(-halfWidth, zero, zero),
            "topCenter" : vector(halfWidth, zero, zero),
            "radius" : tireCoreRadius
        });

        // Rim disk
        fCylinder(context, id + "rimDisk", {
            "bottomCenter" : vector(-halfWidth, zero, zero),
            "topCenter" : vector(halfWidth, zero, zero),
            "radius" : definition.rimDiameter / 2
        });

        // Hub
        fCylinder(context, id + "hub", {
            "bottomCenter" : vector(-hubHalfWidth, zero, zero),
            "topCenter" : vector(hubHalfWidth, zero, zero),
            "radius" : definition.hubDiameter / 2
        });

        // Tread lobes
        for (var i = 0; i < definition.treadCount; i += 1)
        {
            const a = i * 360 * degree / definition.treadCount;
            const y = tireCoreRadius * cos(a);
            const z = tireCoreRadius * sin(a);

            fCylinder(context, id + ("tread" ~ i), {
                "bottomCenter" : vector(-halfWidth, y, z),
                "topCenter" : vector(halfWidth, y, z),
                "radius" : definition.treadDepth
            });
        }

        // Join all solid bodies into one wheel body
        var unionBodies = [
            qCreatedBy(id + "tireCore", EntityType.BODY),
            qCreatedBy(id + "rimDisk", EntityType.BODY),
            qCreatedBy(id + "hub", EntityType.BODY)
        ];

        for (var u = 0; u < definition.treadCount; u += 1)
        {
            unionBodies = append(unionBodies, qCreatedBy(id + ("tread" ~ u), EntityType.BODY));
        }

        opBoolean(context, id + "joinWheel", {
            "tools" : qUnion(unionBodies),
            "operationType" : BooleanOperationType.UNION
        });

        // Cut axle hole
        const cutHalfWidth = definition.hubWidth + definition.wheelWidth + 5 * millimeter;

        fCylinder(context, id + "axleHole", {
            "bottomCenter" : vector(-cutHalfWidth, zero, zero),
            "topCenter" : vector(cutHalfWidth, zero, zero),
            "radius" : definition.axleHoleDiameter / 2
        });

        opBoolean(context, id + "cutAxleHole", {
            "tools" : qCreatedBy(id + "axleHole", EntityType.BODY),
            "targets" : qCreatedBy(id + "tireCore", EntityType.BODY),
            "operationType" : BooleanOperationType.SUBTRACTION
        });

        // Outer lightening holes
        const outerHoleRadius = definition.outerHoleDiameter / 2;
        const outerHolePitchRadius = definition.outerHolePitchDiameter / 2;

        for (var j = 0; j < definition.outerHoleCount; j += 1)
        {
            const b = j * 360 * degree / definition.outerHoleCount;
            const yOuter = outerHolePitchRadius * cos(b);
            const zOuter = outerHolePitchRadius * sin(b);

            fCylinder(context, id + ("outerHole" ~ j), {
                "bottomCenter" : vector(-cutHalfWidth, yOuter, zOuter),
                "topCenter" : vector(cutHalfWidth, yOuter, zOuter),
                "radius" : outerHoleRadius
            });

            opBoolean(context, id + ("cutOuterHole" ~ j), {
                "tools" : qCreatedBy(id + ("outerHole" ~ j), EntityType.BODY),
                "targets" : qCreatedBy(id + "tireCore", EntityType.BODY),
                "operationType" : BooleanOperationType.SUBTRACTION
            });
        }

        // Mounting holes around hub
        const mountHoleRadius = definition.mountHoleDiameter / 2;
        const mountPitchRadius = definition.mountHolePitchDiameter / 2;

        for (var m = 0; m < definition.mountHoleCount; m += 1)
        {
            const c = 45 * degree + m * 360 * degree / definition.mountHoleCount;
            const yMount = mountPitchRadius * cos(c);
            const zMount = mountPitchRadius * sin(c);

            fCylinder(context, id + ("mountHole" ~ m), {
                "bottomCenter" : vector(-cutHalfWidth, yMount, zMount),
                "topCenter" : vector(cutHalfWidth, yMount, zMount),
                "radius" : mountHoleRadius
            });

            opBoolean(context, id + ("cutMountHole" ~ m), {
                "tools" : qCreatedBy(id + ("mountHole" ~ m), EntityType.BODY),
                "targets" : qCreatedBy(id + "tireCore", EntityType.BODY),
                "operationType" : BooleanOperationType.SUBTRACTION
            });
        }
    });