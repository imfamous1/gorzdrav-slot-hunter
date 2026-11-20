import QtQuick

Canvas {
    id: icon
    property string glyph
    property color tint: "#152A35"
    implicitWidth: 20
    implicitHeight: 20
    onGlyphChanged: requestPaint()
    onTintChanged: requestPaint()
    onPaint: {
        const c = getContext("2d")
        c.reset()
        c.scale(width / 24, height / 24)
        c.strokeStyle = tint
        c.lineWidth = 1.7
        c.lineCap = "round"
        c.lineJoin = "round"
        function path(points) {
            c.moveTo(points[0], points[1])
            for (let i = 2; i < points.length; i += 2)
                c.lineTo(points[i], points[i + 1])
        }
        function circle(x, y, radius) {
            c.moveTo(x + radius, y)
            c.arc(x, y, radius, 0, 2 * Math.PI)
        }
        c.beginPath()
        switch (glyph) {
        case "cross":
            path([12, 5, 12, 19]); path([5, 12, 19, 12])
            break
        case "check":
            c.lineWidth = 2.4
            path([5, 12, 10, 17, 19, 7])
            break
        case "search":
            circle(10.5, 10.5, 5.7); path([15, 15, 20, 20])
            break
        case "patient":
            circle(12, 7.5, 3.2)
            c.moveTo(5, 20); c.quadraticCurveTo(5, 14, 12, 14)
            c.quadraticCurveTo(19, 14, 19, 20)
            break
        case "telegram":
            path([3, 10, 21, 4, 17, 20, 11, 14, 3, 10]); path([11, 14, 21, 4])
            break
        case "history":
            circle(12, 12, 8); path([12, 7.5, 12, 12.2, 15.5, 14])
            break
        case "down":
            path([7, 10, 12, 15, 17, 10])
            break
        case "update":
            path([12, 3, 12, 14]); path([7, 9, 12, 14, 17, 9])
            path([4, 15, 4, 20, 20, 20, 20, 15])
            break
        case "info":
            circle(12, 12, 8); path([12, 11, 12, 16]); circle(12, 8, 0.35)
            break
        case "play":
            path([8, 5, 19, 12, 8, 19, 8, 5])
            break
        case "stop":
            c.rect(6.5, 6.5, 11, 11)
            break
        }
        c.stroke()
    }
}
