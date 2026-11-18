import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as T

ColumnLayout {
    id: field
    property string title
    property string value
    property string placeholder: ""
    property bool secret: false
    property bool invalid: false
    signal edited(string value)
    function focusInput() { input.forceActiveFocus() }

    spacing: 7
    Text {
        text: field.title
        color: T.ink
        font.pixelSize: 13
        font.weight: Font.DemiBold
    }
    TextField {
        id: input
        Layout.fillWidth: true
        Layout.minimumWidth: 38
        implicitHeight: 43
        leftPadding: 12
        rightPadding: 12
        text: field.value
        placeholderText: field.placeholder
        placeholderTextColor: T.quiet
        color: enabled ? T.ink : T.muted
        selectByMouse: true
        font.pixelSize: 13
        echoMode: field.secret ? TextInput.Password : TextInput.Normal
        Accessible.name: field.title
        onTextEdited: field.edited(text)
        background: Rectangle {
            radius: 10
            antialiasing: true
            color: input.enabled ? T.surface : T.input
            border.width: input.activeFocus || field.invalid ? 2 : 1
            border.color: field.invalid ? T.danger : input.activeFocus ? T.accent : T.border
        }
    }
}
