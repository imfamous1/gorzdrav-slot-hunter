import QtQuick
import QtQuick.Layouts
import "Theme.js" as T

Card {
    id: card
    required property var visit
    property string busy
    signal calendarRequested()
    signal cancellationRequested()
    readonly property bool wide: width >= 730
    padding: 15

    component Detail: Text {
        Layout.fillWidth: true
        color: T.muted
        textFormat: Text.PlainText
        font.pixelSize: 13
        wrapMode: Text.Wrap
    }

    contentItem: GridLayout {
        columns: card.wide ? 3 : 2
        columnSpacing: 16
        rowSpacing: 10

        Rectangle {
            Layout.row: 0
            Layout.column: 0
            Layout.alignment: Qt.AlignTop
            implicitWidth: 104
            implicitHeight: dateTime.implicitHeight + 18
            radius: 11
            color: T.accentSoft
            ColumnLayout {
                id: dateTime
                anchors.centerIn: parent
                spacing: 1
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: card.visit.date
                    font.pixelSize: 12
                    color: T.accent
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: card.visit.time
                    font.pixelSize: 24
                    font.weight: Font.Bold
                    color: T.ink
                }
            }
        }
        ColumnLayout {
            Layout.row: 0
            Layout.column: 1
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.alignment: Qt.AlignTop
            spacing: 4
            Detail { text: card.visit.title; color: T.ink; font.pixelSize: 16; font.weight: Font.Bold }
            Detail { text: card.visit.doctor; visible: text.length > 0 }
            Detail { text: card.visit.clinic; color: T.ink; font.weight: Font.DemiBold }
            Detail { text: card.visit.place; visible: text.length > 0 }
            Detail { text: card.visit.patient; font.pixelSize: 12 }
        }
        RowLayout {
            Layout.row: card.wide ? 0 : 1
            Layout.column: card.wide ? 2 : 1
            Layout.alignment: card.wide ? Qt.AlignRight | Qt.AlignVCenter : Qt.AlignLeft
            spacing: 7
            AppButton {
                objectName: "addVisitToCalendar"
                compact: true
                enabled: card.busy !== card.visit.id
                glyph: "calendar-plus"
                text: "В календарь"
                onClicked: card.calendarRequested()
            }
            AppButton {
                objectName: "cancelVisit"
                compact: true
                enabled: !card.busy
                glyph: card.visit.portalCancel ? "external-link" : "calendar-x"
                text: card.busy === card.visit.id ? "Проверяем…"
                    : card.visit.portalCancel ? "Отмена на сайте" : "Отменить"
                onClicked: card.cancellationRequested()
            }
        }
    }
}
