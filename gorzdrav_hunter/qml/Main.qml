import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Theme.js" as T

ApplicationWindow {
    id: win
    required property var bridge
    readonly property var viewData: bridge.view
    readonly property var update: bridge.update
    property int page: 0
    onPageChanged: if (page === 3) bridge.refreshVisits()
    readonly property var sections: [
        { name: "Поиск", title: "Поиск талонов", key: "search", icon: "search" },
        { name: "Пациенты", title: "Пациенты", key: "patient", icon: "user-round" },
        { name: "Telegram", title: "Telegram", key: "telegram", icon: "send" },
        { name: "Мои записи", title: "Мои записи", key: "visits", icon: "calendar-days" }
    ]
    readonly property var notice: bridge.notices[sections[page].key] || ({})
    readonly property bool searchActive: ["starting", "running", "retry", "stopping"].includes(win.viewData.phase)
    readonly property bool checkingCard: win.viewData.phase === "checking_card"
    readonly property bool attachmentDenied: win.page === 0 && win.viewData.phase === "attachment_denied"
    readonly property bool searchWarning: win.viewData.phase === "retry" || win.checkingCard
    readonly property bool searchError: win.viewData.phase === "failed" || win.attachmentDenied
    readonly property color phaseColor: win.viewData.phase === "running" || win.viewData.phase === "completed" ? T.accent
                      : win.searchWarning ? T.warning : win.searchError ? T.danger : T.muted
    readonly property color phaseBackground: win.viewData.phase === "running" || win.viewData.phase === "completed" ? T.accentSoft
                           : win.searchWarning ? T.warningSoft : win.searchError ? T.dangerSoft : T.neutralSoft

    title: "Талоны Горздрава"
    width: 1120
    height: 756
    minimumWidth: 820
    minimumHeight: 570
    visible: true
    color: T.canvas
    font.family: "Arial"

    component Heading: Text {
        color: T.ink
        font.pixelSize: 18
        font.weight: Font.Bold
    }

    component SecondaryText: Text {
        color: T.muted
        font.pixelSize: 13
        wrapMode: Text.WordWrap
    }

    component Mode: RowLayout {
        id: option
        property string key
        property string title
        property string details
        spacing: 7
        AppButton {
            text: option.title
            Layout.fillWidth: true
            enabled: win.viewData.editable
            selected: win.viewData.mode === option.key
            onClicked: win.bridge.setSearch("mode", option.key)
        }
        AppButton {
            id: info
            compact: true
            glyph: "circle-question-mark"
            label: "О режиме «" + option.title + "»"
            Layout.preferredHeight: 44
            Layout.preferredWidth: 44
            selected: help.opened
            ToolTip.text: label
            ToolTip.delay: 400
            ToolTip.visible: !help.visible && (hovered || visualFocus)
            onClicked: help.opened ? help.close() : help.open()
            Popup {
                id: help
                x: info.width - width
                y: info.height + 7
                width: 292
                padding: 16
                margins: 14
                focus: true
                closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                background: Rectangle { radius: 13; border.color: T.border; color: T.surface }
                contentItem: ColumnLayout {
                    spacing: 8
                    Heading { text: option.title; font.pixelSize: 15 }
                    SecondaryText { text: option.details; Layout.fillWidth: true }
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0
        Rectangle {
            id: rail
            objectName: "navigationRail"
            readonly property int inset: 16
            Layout.preferredWidth: 206
            Layout.fillHeight: true
            color: T.sidebar

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: rail.inset
                anchors.rightMargin: rail.inset
                anchors.topMargin: 25
                anchors.bottomMargin: 23
                spacing: 0

                ColumnLayout {
                    objectName: "navigationBrand"
                    spacing: 3
                    Layout.fillWidth: true
                    Layout.minimumHeight: 37
                    Layout.bottomMargin: 30
                    Text {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Талоны Горздрава"
                        color: T.surface
                        font.pixelSize: 13
                        font.weight: Font.Bold
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: "Санкт-Петербург"
                        color: T.railSoft
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                    }
                }
                Repeater {
                    model: win.sections
                    delegate: Button {
                        id: nav
                        required property var modelData
                        required property int index
                        readonly property bool selected: win.page === index
                        text: modelData.name
                        objectName: "nav_" + modelData.key
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Layout.bottomMargin: 5
                        implicitHeight: 43
                        leftPadding: 11
                        rightPadding: 10
                        hoverEnabled: true
                        onClicked: win.page = index
                        background: Rectangle {
                            radius: 10
                            color: nav.selected ? T.railActive : nav.hovered ? T.railHover : "transparent"
                            border.width: nav.visualFocus ? 2 : 0
                            border.color: T.railHighlight
                        }
                        contentItem: RowLayout {
                            spacing: 9
                            Icon {
                                glyph: nav.modelData.icon
                                tint: nav.selected ? T.railHighlight : T.railSoft
                                Layout.preferredHeight: 18
                                Layout.preferredWidth: 18
                            }
                            Text {
                                Layout.fillWidth: true
                                text: nav.text
                                color: nav.selected ? T.surface : T.railText
                                font.pixelSize: 13
                                font.weight: nav.selected ? Font.Bold : Font.Medium
                            }
                            Rectangle {
                                visible: nav.selected
                                Layout.preferredWidth: 5
                                Layout.preferredHeight: 5
                                radius: 3
                                color: T.railHighlight
                            }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 1
                    Layout.bottomMargin: 13
                    color: T.railBorder
                }
                Text {
                    objectName: "supportCaption"
                    Layout.fillWidth: true
                    text: "Создано с любовью к автоматизации.\nЖелаю всем здоровья!"
                    color: T.railText
                    font.pixelSize: 12
                    lineHeight: 1.25
                    wrapMode: Text.WordWrap
                }
                Button {
                    id: supportButton
                    objectName: "supportProject"
                    Layout.fillWidth: true
                    Layout.topMargin: 10
                    implicitHeight: 34
                    leftPadding: 8
                    rightPadding: 8
                    hoverEnabled: true
                    text: "Поддержать проект"
                    Accessible.description: "Откроется страница CloudTips в браузере"
                    onClicked: Qt.openUrlExternally("https://pay.cloudtips.ru/p/433a83a0")
                    background: Rectangle {
                        radius: 8
                        color: supportButton.hovered || supportButton.down ? T.railActive : T.railHover
                        border.width: supportButton.visualFocus ? 2 : 0
                        border.color: T.railHighlight
                    }
                    contentItem: RowLayout {
                        spacing: 8
                        Text {
                            Layout.fillWidth: true
                            text: supportButton.text
                            color: T.surface
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Icon {
                            objectName: "supportIcon"
                            glyph: "heart"
                            tint: T.railHighlight
                            Layout.preferredWidth: 16
                            Layout.preferredHeight: 16
                        }
                    }
                }
                Button {
                    id: updateButton
                    objectName: "updatesMenu"
                    readonly property bool busy: win.update.state === "checking" || win.update.state === "installing"
                    Layout.fillWidth: true
                    Layout.topMargin: 3
                    implicitHeight: 36
                    leftPadding: 8
                    rightPadding: 8
                    hoverEnabled: true
                    text: "Версия " + win.update.version
                    Accessible.name: text + ". Обновления"
                    Accessible.description: win.update.hasRelease ? "Доступна версия " + win.update.available : ""
                    onClicked: updatePopup.open()
                    ToolTip.visible: !updatePopup.visible && (hovered || visualFocus)
                    ToolTip.delay: 400
                    ToolTip.text: win.update.hasRelease ? "Доступна версия " + win.update.available : "Обновления приложения"
                    background: Rectangle {
                        radius: 8
                        color: updateButton.hovered || updatePopup.visible || win.update.hasRelease ? T.railActive : "transparent"
                        border.width: updateButton.visualFocus ? 2 : 0
                        border.color: T.railHighlight
                    }
                    contentItem: RowLayout {
                        spacing: 6
                        Text {
                            Layout.fillWidth: true
                            text: updateButton.text
                            color: T.railText
                            font.pixelSize: 12
                        }
                        Text {
                            visible: win.update.hasRelease
                            text: "Обновить"
                            color: T.railHighlight
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Icon {
                            objectName: "updatesIcon"
                            glyph: win.update.hasRelease ? "download" : "refresh-cw"
                            tint: win.update.hasRelease ? T.railHighlight : T.railSoft
                            Layout.preferredWidth: 16
                            Layout.preferredHeight: 16
                        }
                    }
                    Popup {
                        id: updatePopup
                        x: updateButton.width + 12
                        y: updateButton.height - height
                        width: 310
                        padding: 16
                        margins: 12
                        focus: true
                        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
                        onClosed: updateButton.forceActiveFocus()
                        background: Rectangle { radius: 13; color: T.surface; border.color: T.border }
                        contentItem: ColumnLayout {
                            spacing: 10
                            Heading {
                                text: win.update.hasRelease ? "Доступна версия " + win.update.available : "Обновления"
                                font.pixelSize: 16
                            }
                            SecondaryText {
                                objectName: "updateStatus"
                                Layout.fillWidth: true
                                text: win.update.message || "Установлена версия " + win.update.version + "."
                                color: win.update.state === "error" ? T.danger : T.muted
                            }
                            AppButton {
                                objectName: "updateAction"
                                Layout.fillWidth: true
                                enabled: !updateButton.busy
                                variant: "primary"
                                text: win.update.hasRelease ? "Скачать и установить" : "Проверить обновления"
                                onClicked: {
                                    if (win.update.hasRelease)
                                        win.bridge.installUpdate()
                                    else
                                        win.bridge.checkForUpdate()
                                }
                            }
                            AppButton {
                                Layout.alignment: Qt.AlignLeft
                                compact: true
                                text: win.update.hasRelease ? "Что изменилось на GitHub" : "Релизы на GitHub"
                                onClicked: Qt.openUrlExternally(win.update.url)
                            }
                        }
                    }
                }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ColumnLayout {
                anchors.fill: parent
                anchors.leftMargin: 27
                anchors.rightMargin: 27
                anchors.topMargin: 23
                anchors.bottomMargin: 16
                spacing: 15

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 55
                    Text {
                        text: win.sections[win.page].title
                        color: T.ink
                        font.pixelSize: 32
                        font.weight: Font.Bold
                    }
                    Item { Layout.fillWidth: true }
                    AppButton {
                        objectName: "refreshVisits"
                        visible: win.page === 3
                        enabled: win.bridge.visits.available && !win.bridge.visits.busy
                        compact: true
                        glyph: "refresh-cw"
                        text: win.bridge.visits.busy === "refresh" ? "Обновляем…" : "Обновить"
                        onClicked: win.bridge.refreshVisits()
                    }
                    AppButton {
                        objectName: "visitsPortal"
                        visible: win.page === 3
                        compact: true
                        glyph: "external-link"
                        text: "Открыть Горздрав"
                        onClicked: win.bridge.openVisitsPortal()
                    }
                    ColumnLayout {
                        visible: win.page === 0
                        Layout.fillWidth: false
                        Layout.alignment: Qt.AlignRight | Qt.AlignVCenter
                        spacing: 4
                        Rectangle {
                            Layout.alignment: Qt.AlignRight
                            implicitWidth: stateLabel.implicitWidth + 26
                            implicitHeight: 30
                            radius: 15
                            color: win.phaseBackground
                            RowLayout {
                                id: stateLabel
                                anchors.centerIn: parent
                                spacing: 6
                                Rectangle {
                                    Layout.preferredWidth: 6
                                    Layout.preferredHeight: 6
                                    radius: 3
                                    color: win.phaseColor
                                }
                                Text {
                                    objectName: "searchStatus"
                                    text: win.viewData.status
                                    color: win.phaseColor
                                    font.pixelSize: 12
                                    font.weight: Font.Bold
                                }
                            }
                        }
                        SecondaryText {
                            objectName: "searchPoll"
                            Layout.alignment: Qt.AlignRight
                            visible: Boolean(win.viewData.lastPoll) || !win.attachmentDenied
                            text: win.viewData.lastPoll ? "Последняя проверка в " + win.viewData.lastPoll : "Проверок пока нет"
                            font.pixelSize: 12
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    visible: win.page === 0
                    spacing: 8
                    ListView {
                        id: tabList
                        Layout.fillWidth: true
                        Layout.preferredHeight: 40
                        model: win.viewData.tabs
                        clip: true
                        orientation: ListView.Horizontal
                        spacing: 6
                        keyNavigationEnabled: false
                        function showSelection() {
                            for (let i = 0; i < model.length; ++i) {
                                if (model[i].id === win.viewData.selected) {
                                    positionViewAtIndex(i, ListView.Contain)
                                    return
                                }
                            }
                        }
                        onModelChanged: Qt.callLater(showSelection)
                        ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded; height: 3 }
                        delegate: Button {
                            id: tab
                            required property var modelData
                            required property int index
                            readonly property bool selected: win.viewData.selected === modelData.id
                            width: Math.max(100, Math.min(213, tabText.implicitWidth + 29))
                            height: 38
                            text: modelData.name
                            objectName: "searchTab_" + modelData.id
                            hoverEnabled: true
                            onClicked: win.bridge.selectSearch(modelData.id)
                            onActiveFocusChanged: if (activeFocus) tabList.positionViewAtIndex(index, ListView.Contain)
                            ToolTip.text: text
                            ToolTip.visible: hovered && tabText.truncated
                            ToolTip.delay: 650
                            contentItem: Text {
                                id: tabText
                                text: tab.text
                                color: tab.selected ? T.accent : T.muted
                                elide: Text.ElideRight
                                verticalAlignment: Text.AlignVCenter
                                horizontalAlignment: Text.AlignHCenter
                                font.pixelSize: 13
                                font.weight: tab.selected ? Font.Bold : Font.Medium
                            }
                            background: Rectangle {
                                radius: 10
                                color: tab.selected ? T.accentSoft : tab.hovered ? T.input : T.surface
                                border.width: tab.visualFocus ? 2 : 1
                                border.color: tab.selected || tab.visualFocus ? T.accent : T.border
                            }
                        }
                    }
                    AppButton {
                        objectName: "newSearch"
                        compact: true
                        glyph: "list-plus"
                        text: "Новый поиск"
                        onClicked: win.bridge.newSearch()
                    }
                }

                ScrollView {
                    id: workScroll
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    contentWidth: availableWidth
                    clip: true
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    ScrollBar.vertical: ScrollBar {
                        policy: ScrollBar.AsNeeded
                        width: 5
                        contentItem: Rectangle { radius: 3; color: T.scrollThumb }
                    }
                    Loader {
                        id: pageLoader
                        width: workScroll.availableWidth
                        sourceComponent: [searchPage, patientPage, telegramPage, visitsPage][win.page]
                        onSourceComponentChanged: workScroll.contentItem.contentY = 0
                    }
                }

                Rectangle {
                    visible: win.page === 0 || Boolean(win.notice.text)
                    Layout.fillWidth: true
                    color: "transparent"
                    implicitHeight: footerContent.implicitHeight
                    GridLayout {
                        id: footerContent
                        anchors.fill: parent
                        columns: 3
                        columnSpacing: 12
                        rowSpacing: 8
                        readonly property bool stacked: win.page === 0 && searchFeedback.text.length > 0 && width < 730
                        SecondaryText {
                            id: searchFeedback
                            objectName: "searchFeedback"
                            Layout.row: 0
                            Layout.column: 0
                            Layout.columnSpan: footerContent.stacked ? 3 : 1
                            Layout.minimumWidth: 0
                            Layout.fillWidth: true
                            Layout.alignment: Qt.AlignVCenter
                            color: win.notice.error || win.attachmentDenied ||
                                   (win.page === 0 && win.viewData.cardError) ? T.danger : T.muted
                            textFormat: Text.PlainText
                            text: win.notice.text || (win.page === 0 &&
                                (win.viewData.cardError || (win.attachmentDenied &&
                                  "Горздрав отклонил запись из-за данных о прикреплении по ОМС. При необходимости уточни их в регистратуре."))) || ""
                        }
                        Item {
                            visible: footerContent.stacked
                            Layout.row: 1
                            Layout.column: 0
                            Layout.fillWidth: true
                        }
                        AppButton {
                            objectName: "attachmentCheck"
                            visible: win.page === 0
                            Layout.row: footerContent.stacked ? 1 : 0
                            Layout.column: 1
                            glyph: "external-link"
                            text: "Проверить прикрепление"
                            onClicked: win.bridge.openAttachmentCheck()
                        }
                        AppButton {
                            objectName: "searchAction"
                            visible: win.page === 0
                            Layout.row: footerContent.stacked ? 1 : 0
                            Layout.column: 2
                            enabled: win.viewData.phase !== "stopping" && !win.checkingCard
                            variant: win.searchActive ? "danger" : "primary"
                            glyph: win.searchActive ? "search-x" : "search"
                            Layout.preferredWidth: 188
                            text: win.searchActive ? "Остановить поиск" : "Запустить поиск"
                            onClicked: win.bridge.searchAction()
                        }
                    }
                }
            }
        }
    }

    Component {
        id: searchPage
        GridLayout {
            id: search
            columns: win.width >= 1040 ? 2 : 1
            columnSpacing: 13
            rowSpacing: 13
            function invalid(field) { return win.notice.field === field && win.notice.error === true }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.preferredWidth: 415
                Layout.alignment: Qt.AlignTop
                spacing: 13

                Card {
                    Layout.fillWidth: true
                    contentItem: ColumnLayout {
                        spacing: 14
                        Heading { text: "Поликлиника и врач" }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: 2
                            columnSpacing: 12
                            rowSpacing: 12
                            Picker {
                                id: district
                                objectName: "district"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 150
                                bridge: win.bridge
                                title: "Район"; placeholder: "Выбрать район"
                                value: win.viewData.choices.district || ""
                                options: win.viewData.options.district || []
                                enabled: win.viewData.editable
                                invalid: search.invalid("district")
                                onChosen: value => win.bridge.choose("district", value)
                            }
                            Picker {
                                id: clinic
                                objectName: "clinic"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 223
                                bridge: win.bridge
                                title: "Поликлиника"; placeholder: "Выбрать поликлинику"
                                value: win.viewData.choices.clinic || ""
                                options: win.viewData.options.clinic || []
                                enabled: win.viewData.editable && Boolean(win.viewData.choices.district)
                                invalid: search.invalid("clinic")
                                onChosen: value => win.bridge.choose("clinic", value)
                            }
                            Picker {
                                id: specialty
                                objectName: "specialty"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 180
                                bridge: win.bridge
                                title: "Специальность"; placeholder: "Выбрать специальность"
                                value: win.viewData.choices.specialty || ""
                                options: win.viewData.options.specialty || []
                                enabled: win.viewData.editable && Boolean(win.viewData.choices.clinic)
                                invalid: search.invalid("specialty")
                                onChosen: value => win.bridge.choose("specialty", value)
                            }
                            Picker {
                                objectName: "doctor"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 180
                                bridge: win.bridge
                                title: "Врач"; placeholder: "Любой врач"
                                value: win.viewData.choices.doctor || ""
                                options: win.viewData.options.doctor || []
                                enabled: win.viewData.editable && Boolean(win.viewData.choices.specialty)
                                onChosen: value => win.bridge.choose("doctor", value)
                            }
                        }
                        RowLayout {
                            visible: win.viewData.catalogNote.length > 0
                            Layout.fillWidth: true
                            SecondaryText { Layout.fillWidth: true; text: win.viewData.catalogNote }
                            AppButton {
                                visible: win.viewData.catalogError
                                compact: true
                                text: "Повторить"
                                onClicked: win.bridge.retryCatalog()
                            }
                        }
                    }
                }

                Card {
                    Layout.fillWidth: true
                    contentItem: ColumnLayout {
                        spacing: 12
                        Heading { text: "Даты поиска" }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 9
                            Picker {
                                readonly property var periods: [
                                    { id: "from", name: "С выбранной даты" },
                                    { id: "range", name: "Диапазон" },
                                    { id: "day", name: "Один день" }
                                ]
                                Layout.fillWidth: true
                                Layout.preferredWidth: 130
                                bridge: win.bridge
                                title: "Период"
                                options: periods
                                searchable: false
                                value: (periods.find(item => item.id === win.viewData.period) || periods[0]).name
                                enabled: win.viewData.editable
                                onChosen: value => win.bridge.setSearch("period", value)
                            }
                            FormInput {
                                id: start
                                objectName: "start"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 108
                                title: win.viewData.period === "day" ? "Дата" : "С"
                                placeholder: "ДД.ММ.ГГГГ"
                                value: win.viewData.start
                                enabled: win.viewData.editable
                                invalid: search.invalid("start")
                                onEdited: value => win.bridge.setSearch("start", value)
                            }
                            FormInput {
                                id: end
                                objectName: "end"
                                visible: win.viewData.period === "range"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 108
                                title: "По"
                                placeholder: "ДД.ММ.ГГГГ"
                                value: win.viewData.end
                                enabled: win.viewData.editable
                                invalid: search.invalid("end")
                                onEdited: value => win.bridge.setSearch("end", value)
                            }
                        }
                        AppCheckBox {
                            id: limitTime
                            objectName: "limitSearchTime"
                            readonly property string warning: "Ограничение по времени может снизить шансы поймать талон."
                            Layout.fillWidth: true
                            text: "Ограничить время приёма"
                            checked: win.viewData.timeLimited
                            enabled: win.viewData.editable
                            Accessible.description: warning
                            ToolTip.visible: enabled && !checked && (hovered || visualFocus)
                            ToolTip.delay: 400
                            ToolTip.text: warning
                            onToggled: win.bridge.limitSearchTime(checked)
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            visible: win.viewData.timeLimited
                            spacing: 9
                            FormInput {
                                id: timeFrom
                                objectName: "searchTimeFrom"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 108
                                title: "Время с"
                                numericMask: "00:00;_"
                                value: win.viewData.timeFrom
                                enabled: win.viewData.editable
                                invalid: search.invalid("time_from")
                                onEdited: value => win.bridge.setSearch("time_from", value)
                            }
                            FormInput {
                                id: timeTo
                                objectName: "searchTimeTo"
                                Layout.fillWidth: true
                                Layout.preferredWidth: 108
                                title: "Время до"
                                numericMask: "00:00;_"
                                value: win.viewData.timeTo
                                enabled: win.viewData.editable
                                invalid: search.invalid("time_to")
                                onEdited: value => win.bridge.setSearch("time_to", value)
                            }
                        }
                        SecondaryText {
                            objectName: "searchTimeHint"
                            Layout.fillWidth: true
                            visible: win.viewData.timeLimited && win.viewData.editable
                            text: limitTime.warning
                        }
                    }
                }
            }

            Card {
                Layout.fillWidth: true
                Layout.preferredWidth: 342
                Layout.alignment: Qt.AlignTop
                contentItem: ColumnLayout {
                    spacing: 13
                    Heading { text: "Режим и пациент" }
                    Mode {
                        id: bookMode
                        Layout.fillWidth: true
                        key: "book"
                        title: "Автозапись"
                        details: "Приложение ищет свободный талон по выбранным условиям и пытается записать пациента, когда находит подходящее время."
                    }
                    Mode {
                        Layout.fillWidth: true
                        key: "transfer"
                        title: "Автоперенос"
                        details: "Когда найдётся подходящий талон, приложение попробует перенести текущий приём. Если Горздрав подтвердит перенос, у приёма будут новые дата и время."
                    }
                    SecondaryText {
                        visible: !win.viewData.mode
                        Layout.fillWidth: true
                        text: "Для этой сохранённой вкладки выбери режим."
                    }
                    Picker {
                        id: patient
                        objectName: "searchPatient"
                        Layout.fillWidth: true
                        bridge: win.bridge
                        title: "Пациент"; placeholder: "Выбрать пациента"
                        value: win.viewData.patientLabel
                        options: win.viewData.patients
                        enabled: win.viewData.editable
                        invalid: search.invalid("patient")
                        onChosen: value => win.bridge.choose("patient", value)
                    }
                    Rectangle {
                        visible: win.viewData.mode === "transfer" && !win.viewData.editable
                        Layout.fillWidth: true
                        implicitHeight: sourceSummary.implicitHeight + 24
                        radius: 12
                        color: T.accentSoft
                        ColumnLayout {
                            id: sourceSummary
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 5
                            SecondaryText { text: "Исходный приём"; color: T.accent; font.pixelSize: 12 }
                            Heading { text: win.viewData.choices.source || "—"; font.pixelSize: 17 }
                        }
                    }
                    Picker {
                        id: source
                        objectName: "source"
                        visible: win.viewData.mode === "transfer" && win.viewData.editable
                        Layout.fillWidth: true
                        bridge: win.bridge
                        title: "Исходный приём"; placeholder: "Выбрать после проверки"
                        value: win.viewData.choices.source || ""
                        options: win.viewData.options.source || []
                        searchable: false
                        enabled: options.length > 0 && !win.viewData.sourceBusy
                        invalid: search.invalid("source")
                        onChosen: value => win.bridge.choose("source", value)
                    }
                    AppButton {
                        id: find
                        objectName: "findTransfer"
                        visible: source.visible
                        enabled: !win.viewData.sourceBusy
                        glyph: "search"
                        text: win.viewData.sourceBusy ? "Проверяем…" : "Найти запись"
                        onClicked: win.bridge.findTransfer()
                    }
                    SecondaryText {
                        visible: source.visible && Boolean(win.viewData.sourceNote)
                        Layout.fillWidth: true
                        text: win.viewData.sourceNote
                    }
                }
            }
            Connections {
                target: win.bridge
                function onFocusRequested(field) {
                    switch (field) {
                    case "district": district.focusInput(); break
                    case "clinic": clinic.focusInput(); break
                    case "specialty": specialty.focusInput(); break
                    case "start": start.focusInput(); break
                    case "end": end.focusInput(); break
                    case "time_from": timeFrom.focusInput(); break
                    case "time_to": timeTo.focusInput(); break
                    case "patient": patient.focusInput(); break
                    case "source": find.forceActiveFocus(); break
                    }
                }
            }
        }
    }

    Component {
        id: patientPage
        ColumnLayout {
            spacing: 12
            Card {
                objectName: "patientCard"
                Layout.fillWidth: true
                contentItem: ColumnLayout {
                    spacing: 15
                    RowLayout {
                        Layout.fillWidth: true
                        Picker {
                            Layout.fillWidth: true
                            bridge: win.bridge
                            title: "Профиль"; placeholder: "Новый пациент"
                            value: (win.viewData.patients.find(option => option.id === win.viewData.patientId) || {}).name || ""
                            options: win.viewData.patients
                            onChosen: value => win.bridge.selectPatient(value)
                        }
                        AppButton {
                            Layout.alignment: Qt.AlignBottom
                            glyph: "user-round-plus"
                            text: "Добавить пациента"
                            enabled: win.viewData.canAddPatient
                            onClicked: { win.bridge.selectPatient(""); surname.focusInput() }
                        }
                    }
                    GridLayout {
                        Layout.fillWidth: true
                        enabled: win.viewData.patientEditable
                        columns: 2
                        uniformCellWidths: true
                        columnSpacing: 12
                        rowSpacing: 12
                        FormInput {
                            id: surname
                            objectName: "patientSurname"
                            Layout.fillWidth: true
                            title: "Фамилия"; value: win.viewData.patientForm.last_name
                            invalid: win.notice.field === "last_name"
                            onEdited: value => win.bridge.setPatient("last_name", value)
                        }
                        FormInput {
                            objectName: "patientName"
                            Layout.fillWidth: true
                            title: "Имя"; value: win.viewData.patientForm.first_name
                            invalid: win.notice.field === "first_name"
                            onEdited: value => win.bridge.setPatient("first_name", value)
                        }
                        FormInput {
                            Layout.fillWidth: true
                            title: "Отчество"; placeholder: "Если есть"; value: win.viewData.patientForm.middle_name
                            onEdited: value => win.bridge.setPatient("middle_name", value)
                        }
                        FormInput {
                            id: birthdate
                            objectName: "patientBirthdate"
                            Layout.fillWidth: true
                            title: "Дата рождения"
                            numericMask: "00.00.0000;_"
                            value: win.viewData.patientForm.birthdate
                            invalid: win.notice.field === "birthdate"
                            onEdited: value => win.bridge.setPatient("birthdate", value)
                        }
                    }
                    AppButton {
                        objectName: "savePatient"
                        Layout.alignment: Qt.AlignRight
                        variant: "primary"
                        enabled: win.viewData.patientEditable
                        text: "Сохранить пациента"
                        onClicked: win.bridge.savePatient()
                    }
                }
            }
            Connections {
                target: win.bridge
                function onFocusRequested(field) {
                    if (field === "birthdate")
                        birthdate.focusInput()
                }
            }
        }
    }

    Component {
        id: telegramPage
        ColumnLayout {
            Card {
                objectName: "telegramCard"
                Layout.fillWidth: true
                contentItem: ColumnLayout {
                    spacing: 15
                    Switch {
                        id: notifications
                        objectName: "telegramSwitch"
                        Layout.fillWidth: true
                        text: checked ? "Уведомления о талонах и записи включены"
                                      : "Уведомления о талонах и записи выключены"
                        checked: win.viewData.telegram.enabled
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        palette.windowText: T.ink
                        palette.dark: T.accent
                        palette.midlight: T.quiet
                        palette.mid: T.muted
                        palette.window: T.surface
                        palette.light: T.accentSoft
                        palette.highlight: notifications.checked ? T.accentHover : T.accent
                        onToggled: win.bridge.toggleTelegram(checked)
                    }
                    FormInput {
                        Layout.fillWidth: true
                        title: "Токен бота"; secret: true
                        enabled: win.viewData.telegram.enabled
                        value: win.viewData.telegram.bot_token
                        invalid: win.notice.field === "bot_token"
                        onEdited: value => win.bridge.setTelegram("bot_token", value)
                    }
                    FormInput {
                        Layout.fillWidth: true
                        title: "ID чата"
                        enabled: win.viewData.telegram.enabled
                        value: win.viewData.telegram.chat_id
                        invalid: win.notice.field === "chat_id"
                        onEdited: value => win.bridge.setTelegram("chat_id", value)
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        AppButton {
                            glyph: "send-horizontal"
                            text: win.viewData.telegram.busy ? "Отправляем…" : "Отправить тест"
                            enabled: win.viewData.telegram.enabled && !win.viewData.telegram.busy
                            onClicked: win.bridge.testTelegram()
                        }
                        Item { Layout.fillWidth: true }
                        AppButton { variant: "primary"; text: "Сохранить"; onClicked: win.bridge.saveTelegram() }
                    }
                }
            }
        }
    }

    Component {
        id: visitsPage
        ColumnLayout {
            id: visitsList
            readonly property var visits: win.bridge.visits
            spacing: 10
            SecondaryText {
                visible: visitsList.visits.rows.length > 0
                Layout.fillWidth: true
                text: "Предстоящие приёмы · время Санкт-Петербурга"
            }
            Card {
                Layout.fillWidth: true
                visible: visitsList.visits.rows.length === 0
                contentItem: ColumnLayout {
                    spacing: 8
                    Heading {
                        font.pixelSize: 16
                        text: win.bridge.visits.busy === "refresh" ? "Загружаем записи…"
                            : win.notice.error ? "Талоны недоступны" : "Предстоящих записей нет"
                    }
                    SecondaryText {
                        visible: !win.bridge.visits.busy && !win.notice.error
                        Layout.fillWidth: true
                        text: win.bridge.visits.available
                            ? "Талоны в других поликлиниках можно проверить на Горздраве."
                            : "Здесь появятся талоны из поликлиник, в которых ты запускал поиск."
                    }
                }
            }
            Repeater {
                model: visitsList.visits.rows
                delegate: VisitCard {
                    required property var modelData
                    Layout.fillWidth: true
                    visit: modelData
                    busy: win.bridge.visits.busy
                    onCalendarRequested: win.bridge.addVisitToCalendar(visit.id)
                    onCancellationRequested: win.bridge.requestVisitCancellation(visit.id)
                }
            }
        }
    }

    Popup {
        id: cancelVisit
        objectName: "confirmVisitCancellation"
        property string summary
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: 440
        modal: true
        focus: true
        padding: 22
        closePolicy: Popup.CloseOnEscape
        onOpened: keepVisit.forceActiveFocus()
        onClosed: win.bridge.confirmVisitCancellation(false)
        background: Rectangle { radius: 16; color: T.surface; border.color: T.border }
        contentItem: ColumnLayout {
            spacing: 14
            Heading { text: "Отменить приём?" }
            SecondaryText { Layout.fillWidth: true; text: cancelVisit.summary; textFormat: Text.PlainText }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton { id: keepVisit; text: "Оставить запись"; onClicked: cancelVisit.close() }
                AppButton {
                    objectName: "acceptVisitCancellation"
                    text: "Отменить приём"
                    variant: "danger"
                    onClicked: { win.bridge.confirmVisitCancellation(true); cancelVisit.close() }
                }
            }
        }
    }

    Popup {
        id: confirm
        objectName: "confirmSearch"
        property string headline
        property string summary
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: 440
        modal: true
        focus: true
        padding: 22
        closePolicy: Popup.CloseOnEscape
        onClosed: win.bridge.confirmSearch(false)
        background: Rectangle { radius: 16; color: T.surface; border.color: T.border }
        contentItem: ColumnLayout {
            spacing: 14
            Heading { text: confirm.headline }
            SecondaryText { Layout.fillWidth: true; text: confirm.summary; textFormat: Text.PlainText }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                AppButton { text: "Отмена"; onClicked: confirm.close() }
                AppButton {
                    objectName: "acceptSearch"
                    text: "Подтвердить"
                    variant: "primary"
                    onClicked: { win.bridge.confirmSearch(true); confirm.close() }
                }
            }
        }
    }

    Connections {
        target: win.bridge
        function onConfirmationRequested(title, summary) {
            confirm.headline = title
            confirm.summary = summary
            confirm.open()
        }
        function onVisitCancellationRequested(summary) {
            cancelVisit.summary = summary
            cancelVisit.open()
        }
    }
}
