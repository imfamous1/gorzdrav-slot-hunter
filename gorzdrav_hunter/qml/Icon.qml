import QtQuick
import QtQuick.Window
import "Theme.js" as T

Image {
    property string glyph
    property color tint: T.ink
    width: 20
    height: 20
    source: glyph ? "image://lucide/" + glyph + "/" + tint.toString().slice(1) : ""
    sourceSize: Qt.size(Math.ceil(width * Screen.devicePixelRatio),
                        Math.ceil(height * Screen.devicePixelRatio))
    fillMode: Image.PreserveAspectFit
    Accessible.ignored: true
}
