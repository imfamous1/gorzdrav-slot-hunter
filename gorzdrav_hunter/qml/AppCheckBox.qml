import QtQuick
import QtQuick.Controls
import "Theme.js" as T

CheckBox {
    id: control
    implicitHeight: 32
    padding: 0
    spacing: 9
    hoverEnabled: true
    font.pixelSize: 13
    font.weight: Font.DemiBold
    palette.windowText: enabled ? T.ink : T.muted

    indicator: Rectangle {
        id: box
        implicitWidth: 20
        implicitHeight: 20
        x: control.mirrored ? control.width - width - control.rightPadding : control.leftPadding
        y: control.topPadding + (control.availableHeight - height) / 2
        radius: 5
        antialiasing: true
        color: !control.enabled ? (control.checked ? T.accentSoft : T.input)
               : control.checked ? (control.hovered || control.down ? T.accentHover : T.accent)
               : control.hovered || control.down ? T.accentSoft : T.surface
        border.color: !control.enabled ? T.border : control.checked ? box.color : T.quiet

        Rectangle {
            anchors.fill: parent
            anchors.margins: -3
            visible: control.visualFocus
            radius: 8
            antialiasing: true
            color: "transparent"
            border.color: T.accent
            border.width: 2
        }
        Icon {
            anchors.centerIn: parent
            width: 16
            height: 16
            visible: control.checked
            glyph: "check"
            tint: control.enabled ? T.surface : T.muted
        }
    }
}
