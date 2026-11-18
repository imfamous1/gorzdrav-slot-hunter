import QtQuick
import QtQuick.Controls
import "Theme.js" as T

Pane {
    padding: 22
    background: Rectangle {
        radius: 18
        antialiasing: true
        color: T.surface
        border.color: T.border
    }
}
