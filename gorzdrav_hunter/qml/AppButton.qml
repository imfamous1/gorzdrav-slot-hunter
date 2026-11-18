import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as T

Button {
    id: control
    property string glyph: ""
    property string variant: "secondary"
    property bool selected: false
    property string label: text
    property bool compact: false
    readonly property bool filled: variant === "primary" || variant === "danger"
    readonly property color inkColor: selected ? T.accent
                 : !enabled ? T.quiet : filled ? T.surface : T.ink
    Accessible.name: label
    implicitHeight: compact ? 38 : 44
    implicitWidth: Math.max(compact ? 38 : 84, buttonContent.implicitWidth + (compact ? 16 : 30))
    hoverEnabled: true
    padding: 0
    contentItem: Item {
        RowLayout {
            id: buttonContent
            anchors.centerIn: parent
            spacing: 8
            Icon {
                visible: control.glyph.length > 0
                glyph: control.glyph
                tint: control.inkColor
                Layout.preferredWidth: control.compact ? 18 : 19
                Layout.preferredHeight: Layout.preferredWidth
            }
            Text {
                visible: control.text.length > 0
                text: control.text
                color: control.inkColor
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
        }
    }
    background: Rectangle {
        radius: 11
        antialiasing: true
        color: control.selected ? T.accentSoft
               : !control.enabled ? T.canvas
               : control.variant === "primary" ? (control.hovered || control.down ? T.accentHover : T.accent)
               : control.variant === "danger" ? (control.hovered || control.down ? T.dangerHover : T.danger)
               : control.down ? T.accentSoft
               : control.hovered ? T.input : T.surface
        border.width: control.visualFocus ? 2 : control.filled ? 0 : 1
        border.color: control.visualFocus || control.selected ? T.accent : T.border
    }
}
