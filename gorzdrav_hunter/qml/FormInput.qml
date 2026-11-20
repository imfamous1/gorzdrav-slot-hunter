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
    property string numericMask: ""
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
        objectName: field.objectName ? field.objectName + "Input" : ""
        Layout.fillWidth: true
        Layout.minimumWidth: 38
        implicitHeight: 43
        leftPadding: 12
        rightPadding: 12
        inputMask: field.numericMask
        placeholderText: field.placeholder
        placeholderTextColor: T.quiet
        color: enabled ? T.ink : T.muted
        selectByMouse: true
        font.pixelSize: 13
        echoMode: field.secret ? TextInput.Password : TextInput.Normal
        Accessible.name: field.title
        function resetEmptyMaskCursor() {
            if (inputMask && cursorPosition > 0 && !/[0-9]/.test(text))
                cursorPosition = 0
        }
        function syncValue() {
            // Keep the cursor when a model refresh returns the edit already on screen.
            const shown = inputMask ? getText(0, length) : text
            if (shown !== field.value)
                text = field.value
            resetEmptyMaskCursor()
        }
        Component.onCompleted: syncValue()
        Connections {
            target: field
            function onValueChanged() { input.syncValue() }
        }
        onTextEdited: field.edited(inputMask ? getText(0, length) : text)
        onCursorPositionChanged: resetEmptyMaskCursor()
        onActiveFocusChanged: {
            if (activeFocus)
                resetEmptyMaskCursor()
        }
        background: Rectangle {
            radius: 10
            antialiasing: true
            color: input.enabled ? T.surface : T.input
            border.width: input.activeFocus || field.invalid ? 2 : 1
            border.color: field.invalid ? T.danger : input.activeFocus ? T.accent : T.border
        }
    }
}
