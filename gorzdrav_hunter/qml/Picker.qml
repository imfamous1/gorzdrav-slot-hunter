import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as T

ColumnLayout {
    id: field
    property var bridge
    property string title
    property string value: ""
    property string placeholder: "Выбрать"
    property var options: []
    property bool searchable: true
    property bool invalid: false
    signal chosen(string value)
    function focusInput() { trigger.forceActiveFocus() }

    spacing: 7
    Text {
        text: field.title
        color: T.ink
        font.pixelSize: 13
        font.weight: Font.DemiBold
    }
    Button {
        id: trigger
        objectName: field.objectName ? field.objectName + "Trigger" : ""
        text: field.value || field.placeholder
        Layout.fillWidth: true
        Layout.minimumWidth: 64
        implicitHeight: 43
        leftPadding: 12
        rightPadding: 10
        hoverEnabled: true
        Accessible.name: field.title + ". " + text
        onClicked: {
            if (listPopup.opened)
                listPopup.close()
            else
                listPopup.open()
        }
        contentItem: RowLayout {
            spacing: 6
            Text {
                Layout.fillWidth: true
                text: trigger.text
                color: field.value ? (trigger.enabled ? T.ink : T.muted) : T.quiet
                font.pixelSize: 13
                elide: Text.ElideRight
            }
            Icon {
                glyph: "down"
                tint: T.quiet
                Layout.preferredWidth: 19
                Layout.preferredHeight: 19
            }
        }
        background: Rectangle {
            radius: 10
            antialiasing: true
            color: trigger.enabled ? (trigger.hovered ? T.input : T.surface) : T.input
            border.width: trigger.visualFocus || field.invalid ? 2 : 1
            border.color: field.invalid ? T.danger : trigger.visualFocus || listPopup.opened ? T.accent : T.border
        }
        Popup {
            id: listPopup
            y: trigger.height + 6
            width: Math.max(trigger.width, 244)
            padding: 8
            margins: 12
            focus: true
            closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
            onAboutToShow: filter.clear()
            onOpened: {
                optionsList.currentIndex = optionsList.count > 0 ? 0 : -1
                if (field.searchable) filter.forceActiveFocus()
                else optionsList.forceActiveFocus()
            }
            onClosed: trigger.forceActiveFocus()
            function select(option) {
                if (!option)
                    return
                const id = String(option.id)
                listPopup.close()
                field.chosen(id)
            }
            background: Rectangle {
                color: T.surface
                radius: 12
                border.color: T.border
            }
            contentItem: ColumnLayout {
                spacing: 6
                TextField {
                    id: filter
                    objectName: field.objectName ? field.objectName + "Filter" : ""
                    visible: field.searchable
                    Layout.fillWidth: true
                    implicitHeight: 35
                    font.pixelSize: 13
                    color: T.ink
                    placeholderText: "Введите для поиска"
                    placeholderTextColor: T.quiet
                    selectionColor: T.accent
                    selectedTextColor: T.surface
                    Accessible.name: "Фильтр: " + field.title
                    background: Rectangle {
                        color: T.input
                        radius: 8
                        border.color: filter.activeFocus ? T.accent : T.border
                    }
                    Keys.onDownPressed: optionsList.incrementCurrentIndex()
                    Keys.onUpPressed: optionsList.decrementCurrentIndex()
                    onAccepted: optionsList.chooseCurrent()
                    onTextEdited: optionsList.currentIndex = optionsList.count > 0 ? 0 : -1
                }
                ListView {
                    id: optionsList
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.max(36, Math.min(232, count * 36))
                    clip: true
                    model: field.searchable && field.bridge && filter.text
                           ? field.bridge.filterChoices(field.options, filter.text) : field.options
                    keyNavigationEnabled: true
                    function chooseCurrent() {
                        if (currentItem)
                            listPopup.select(currentItem.modelData)
                    }
                    Keys.onReturnPressed: chooseCurrent()
                    Keys.onEnterPressed: chooseCurrent()
                    delegate: ItemDelegate {
                        required property var modelData
                        objectName: field.objectName ? field.objectName + "Option_" + modelData.id : ""
                        width: ListView.view.width
                        height: 36
                        text: modelData.label || modelData.name
                        highlighted: ListView.isCurrentItem
                        hoverEnabled: true
                        onClicked: listPopup.select(modelData)
                        contentItem: Text {
                            text: parent.text
                            color: T.ink
                            font.pixelSize: 13
                            elide: Text.ElideRight
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 7
                            color: parent.highlighted || parent.hovered ? T.accentSoft : "transparent"
                        }
                    }
                    Text {
                        anchors.centerIn: parent
                        visible: optionsList.count === 0
                        text: "Ничего не найдено"
                        color: T.quiet
                        font.pixelSize: 13
                    }
                    ScrollBar.vertical: ScrollBar {}
                }
            }
        }
    }
}
