"""Проверка настоящего окна Qt Quick на обеих настольных платформах."""
from importlib.util import find_spec
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(find_spec('PySide6'), 'desktop dependencies are not installed')
class DesktopWindowTests(TestCase):
    def test_window(self):
        # Другие тесты используют QCoreApplication, поэтому окно открывается отдельно.
        source = Path(__file__).resolve()
        project = source.parents[1]
        with TemporaryDirectory() as directory:
            subprocess.run(
                [sys.executable, str(source)], cwd=project, check=True, timeout=65,
                env={**os.environ, 'PYTHONPATH': str(project), 'GORZDRAV_HOME': directory,
                     'QT_QUICK_BACKEND': 'software'},
            )


def check_window():
    from datetime import date, timedelta
    from time import monotonic, sleep
    from unittest.mock import MagicMock, patch

    from PySide6.QtCore import QObject, QPoint, QPointF, Qt, QUrl, Slot
    from PySide6.QtGui import QDesktopServices, QInputMethodEvent
    from PySide6.QtTest import QTest

    from gorzdrav_hunter import __version__
    from gorzdrav_hunter.config import CONFIG_PATH, STATE_PATH, _write_json
    from gorzdrav_hunter.desktop import DesktopApp, create_window
    from gorzdrav_hunter.desktop_plan import apply_plan
    from gorzdrav_hunter.desktop_update import Release, UpdateError

    today = date.today()
    visit = (today + timedelta(days=4)).isoformat() + 'T08:20:00'
    start = (today + timedelta(days=8)).isoformat()
    target = {
        'district_id': 1, 'district_name': 'Выборгский',
        'lpu_id': 1, 'lpu_name': 'Учебная поликлиника № 52',
        'specialty_ids': ['eye'], 'specialty_label': 'Офтальмология',
        'book_date_from': start, 'auto_book': True,
    }
    working = {
        'target': {**target, 'book_time_from': '09:00', 'book_time_to': '13:00', 'reschedule_from': {
            'appointment_id': 'example', 'specialty_id': 'eye', 'visit_start': visit,
        }},
        'patient': 'patient', 'consent': True, 'run': {'worker': 'fixture', 'token': 'active'},
    }
    stopped = {'target': {**target, 'auto_book': False}, 'enabled': False}
    _write_json(CONFIG_PATH, apply_plan({}, {
        'patients': {'patient': {'last_name': 'Примерова', 'first_name': 'Анна',
                                 'birthdate': '14.02.1990', 'patient_ids': {'1': 'demo-patient'}}},
        'jobs': {'old': stopped, 'working': working},
    }))
    _write_json(STATE_PATH, {'desktop_jobs': {
        'working': {'phase': 'watching', 'run_token': 'active', 'last_scan': today.isoformat() + 'T12:03:00'},
    }})
    status = {'phase': 'running', 'id': 'fixture', 'live_plan': True, 'per_job': True}

    with patch('gorzdrav_hunter.desktop.read_status', return_value=status):
        application, engine, bridge = create_window(load_catalog=False)
        window = engine.rootObjects()[0]

        def wait_for(condition):
            limit = monotonic() + 6
            while not condition():
                if monotonic() > limit:
                    raise AssertionError('Qt window did not reach the expected state')
                application.processEvents()
                sleep(0.016)

        def locate(name, parent=None):
            if parent is None:
                parent = window.contentItem()
            if parent.objectName() == name:
                return parent
            for child in parent.childItems():
                found = locate(name, child)
                if found is not None:
                    return found
            return None

        def click(name):
            wait_for(lambda: locate(name) is not None and locate(name).isVisible())
            item = locate(name)
            assert item.width() > 0 and item.height() > 0 and item.isEnabled(), name
            point = item.mapToScene(item.boundingRect().center())
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(round(point.x()), round(point.y())))
            application.processEvents()

        def tab_names():
            return [tab['id'] for tab in bridge.view['tabs']]

        def enter_text(item, text):
            event = QInputMethodEvent()
            event.setCommitString(text)
            application.sendEvent(item, event)

        def capture(directory, name):
            if directory:
                QTest.mouseMove(window, QPoint(12, 12))
                QTest.qWait(150)
                image = window.grabWindow()
                assert not image.isNull() and image.save(str(Path(directory) / name))

        wait_for(lambda: window.isExposed() and locate('searchTab_working') is not None)
        rail = locate('navigationRail')
        for name in ('navigationBrand', 'nav_search', 'nav_patient', 'nav_telegram', 'nav_visits',
                     'supportProject', 'updatesMenu'):
            item = locate(name)
            x = rail.mapFromScene(item.mapToScene(QPointF())).x()
            right = rail.width() - x - item.width()
            assert abs(16 - x) < 0.5 and abs(x - right) < 0.5, (name, x, right)
        assert tab_names() == ['working']
        assert locate('searchAction').property('text') == 'Остановить поиск'
        assert locate('searchAction').property('glyph') == 'search-x'
        assert locate('newSearch').property('glyph') == 'list-plus'
        assert locate('limitSearchTime').property('checked')
        assert locate('searchTimeFromInput').property('text') == '09:00'
        assert not locate('searchTimeFromInput').isEnabled()
        assert locate('updatesMenu').property('text') == f'Версия {__version__}'
        support = locate('supportProject')
        assert support.property('text') == 'Поддержать проект'
        assert abs(locate('supportCaption').mapToScene(QPointF()).x()
                   - support.mapToScene(QPointF()).x()) < 1
        assert support.mapToScene(QPointF(0, support.height())).y() <= locate('updatesMenu').mapToScene(QPointF()).y()

        class LinkRecorder(QObject):
            @Slot(QUrl)
            def record(self, url):
                self.url = url.toString()

        recorder = LinkRecorder()
        QDesktopServices.setUrlHandler('https', recorder, 'record')
        try:
            click('supportProject')
            assert recorder.url == 'https://pay.cloudtips.ru/p/433a83a0'
        finally:
            QDesktopServices.unsetUrlHandler('https')

        release = Release('v9.9.9', 'fixture.zip', 1, '0' * 64)
        with patch('gorzdrav_hunter.desktop.check_release', return_value=release), patch(
            'gorzdrav_hunter.desktop.prepare_update', side_effect=UpdateError('Проверка тестовой сборки.'),
        ):
            click('updatesMenu')
            click('updateAction')
            wait_for(lambda: bridge.update['state'] == 'available')
            assert locate('updateAction').property('text') == 'Скачать и установить'
            click('updateAction')
            wait_for(lambda: bridge.update['state'] == 'error')
            assert locate('updateStatus').property('text') == 'Проверка тестовой сборки.'
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait_for(lambda: locate('updateAction') is None or not locate('updateAction').isVisible())
        bridge.update_release = None
        bridge._set_update('idle')

        if preview := os.environ.get('GORZDRAV_QT_PREVIEW'):
            window.grabWindow()
            application.processEvents()
            QTest.qWait(150)
            image = window.grabWindow()
            assert not image.isNull() and image.save(preview), 'Could not capture Qt window'

        click('newSearch')
        wait_for(lambda: len(tab_names()) == 2)
        draft = bridge.selected
        assert bridge.view['phase'] == 'draft'
        assert locate('searchAction').property('text') == 'Запустить поиск'
        assert locate('searchAction').property('glyph') == 'search'
        with patch('gorzdrav_hunter.desktop.QDesktopServices') as desktop_services:
            click('attachmentCheck')
            assert desktop_services.openUrl.call_args.args[0].toString() == 'https://gorzdrav.spb.ru/service-oms-check'
        assert bridge.view['phase'] == 'draft'
        attachment_preview = os.environ.get('GORZDRAV_ATTACHMENT_PREVIEW')
        capture(attachment_preview, 'search-before-start.png')
        assert not locate('limitSearchTime').property('checked')
        assert not locate('searchTimeFromInput').isVisible()
        click('limitSearchTime')
        assert locate('searchTimeHint').isVisible()
        assert 'снизить шансы' in locate('searchTimeHint').property('text')
        time_from = locate('searchTimeFromInput')
        time_from.forceActiveFocus()
        wait_for(time_from.hasActiveFocus)
        for digit in '0930':
            QTest.keyClick(window, ord(digit))
        assert bridge.form.time_from == '09:30', (bridge.form.time_from, time_from.property('cursorPosition'))
        assert time_from.property('displayText') == '09:30'
        bridge.setSearch('start', '05.12.2031')
        click('searchTab_working')
        wait_for(lambda: bridge.selected == 'working')
        assert locate('searchAction').property('text') == 'Остановить поиск'
        click('searchTab_' + draft)
        wait_for(lambda: bridge.selected == draft)
        assert bridge.view['start'] == '05.12.2031'
        assert locate('searchTimeFromInput').property('text') == '09:30'

        bridge.form.options['district'] = [
            {'id': 1, 'name': 'Адмиралтейский'},
            {'id': 2, 'name': 'Выборгский'},
            {'id': 3, 'name': 'Приморский'},
            {'id': 4, 'name': 'Пушкинский'},
        ]
        bridge.changed.emit()
        with patch.object(bridge, '_load_catalog') as load_catalog:
            def filter_district(text):
                click('districtTrigger')
                wait_for(lambda: (field := locate('districtFilter')) is not None and field.hasActiveFocus())
                enter_text(locate('districtFilter'), text)
                wait_for(lambda: locate('districtOption_1') is None)
                QTest.qWait(150)

            filter_district('п')
            click('districtOption_4')
            wait_for(lambda: bridge.view['choices'].get('district') == 'Пушкинский')
            load_catalog.assert_called_once_with(bridge.form, 'clinic')

            filter_district('прим')
            QTest.keyClick(window, Qt.Key.Key_Return)
            wait_for(lambda: bridge.view['choices'].get('district') == 'Приморский')

        click('nav_patient')
        bridge.selectPatient('')
        wait_for(lambda: locate('patientSurnameInput') is not None)
        patient_card = locate('patientCard')
        assert abs(patient_card.width() - patient_card.parentItem().width()) < 1
        name = locate('patientNameInput')
        surname = locate('patientSurnameInput')
        wait_for(lambda: abs(name.width() - surname.width()) < 1)
        name_x = name.mapToScene(QPointF()).x()
        click('patientSurnameInput')
        enter_text(surname, 'W' * 100)
        assert bridge.patient_form['last_name'] == 'W' * 100
        QTest.qWait(150)
        assert abs(name.mapToScene(QPointF()).x() - name_x) < 1
        assert abs(name.width() - surname.width()) < 1
        bridge.setPatient('first_name', 'Анна')
        click('patientBirthdateInput')
        for digit in '29022000':
            QTest.keyClick(window, ord(digit))
        birthdate = locate('patientBirthdateInput')
        assert birthdate.property('displayText') == '29.02.2000', (
            birthdate.property('displayText'), birthdate.property('cursorPosition'), birthdate.hasActiveFocus(),
        )
        assert bridge.patient_form['birthdate'] == '29.02.2000'
        birthdate.setProperty('cursorPosition', 0)
        QTest.keyClick(window, Qt.Key.Key_Delete)
        assert bridge.patient_form['birthdate'] == '_9.02.2000'
        click('savePatient')
        assert bridge.notices['patient']['field'] == 'birthdate'
        assert birthdate.hasActiveFocus()

        click('nav_telegram')
        wait_for(lambda: locate('telegramSwitch') is not None)
        telegram_card = locate('telegramCard')
        assert abs(telegram_card.width() - telegram_card.parentItem().width()) < 1
        toggle = locate('telegramSwitch')
        assert toggle.property('text').endswith('выключены')
        click('telegramSwitch')
        assert bridge.view['telegram']['enabled']
        assert toggle.property('text').endswith('включены')
        click('telegramSwitch')
        assert not bridge.view['telegram']['enabled']
        assert toggle.property('text').endswith('выключены')

        common = {'lpuId': '1', 'lpuShortName': 'Учебная поликлиника № 52',
                  'lpuAddress': 'ул. Примерная, 12', 'patientId': 'demo-patient'}
        soon = {**common, 'appointmentId': 'first', 'visitStart': visit, 'room': '214',
                'specialityRendingConsultation': {'name': 'Офтальмология'},
                'doctorRendingConsultation': {'name': 'Соколова Мария Андреевна'}}
        later = {**common, 'appointmentId': 'second', 'visitStart': start + 'T14:45:00',
                 'lpuShortName': 'Учебная поликлиника № 52 · отделение № 2', 'lpuAddress': 'ул. Учебная, 8',
                 'isAppointmentByReferral': True,
                 'specialityRendingConsultation': {'name': 'Неврология'},
                 'doctorRendingConsultation': {'name': 'Волков Илья Сергеевич'}}
        client = MagicMock()
        client.patient_appointments.side_effect = [[later, soon], [later, soon], [later]]
        client.appointment_cancel.return_value = True
        with patch.object(bridge, '_client', return_value=client), patch(
            'gorzdrav_hunter.desktop.QDesktopServices',
        ) as desktop_services:
            click('nav_visits')
            wait_for(lambda: len(bridge.visits['rows']) == 2)
            assert not locate('searchAction').isVisible()
            assert not locate('attachmentCheck').isVisible()
            assert [row['title'] for row in bridge.visits['rows']] == ['Офтальмология', 'Неврология']
            assert locate('addVisitToCalendar').property('glyph') == 'calendar-plus'
            assert locate('cancelVisit').property('glyph') == 'calendar-x'
            preview_dir = os.environ.get('GORZDRAV_VISITS_PREVIEW')

            capture(preview_dir, 'my-visits.png')
            window.setWidth(window.minimumWidth())
            capture(preview_dir, 'my-visits-compact.png')
            window.setWidth(1120)

            click('visitsPortal')
            assert desktop_services.openUrl.call_args.args[0].toString() == 'https://gorzdrav.spb.ru/lk-visits'

            click('cancelVisit')
            capture(preview_dir, 'cancel-visit.png')
            QTest.keyClick(window, Qt.Key.Key_Escape)
            wait_for(lambda: locate('acceptVisitCancellation') is None
                     or not locate('acceptVisitCancellation').isVisible())
            client.appointment_cancel.assert_not_called()

            click('addVisitToCalendar')
            event_path = Path(desktop_services.openUrl.call_args.args[0].toLocalFile())
            payload = event_path.read_bytes()
            assert b'DTSTART:' + visit[:10].replace('-', '').encode() + b'T052000Z' in payload
            assert b'TRIGGER:-PT1H' in payload
            assert 'Примерова'.encode() not in payload

            click('cancelVisit')
            click('acceptVisitCancellation')
            wait_for(lambda: len(bridge.visits['rows']) == 1)
            client.appointment_cancel.assert_called_once_with({
                'appointmentId': 'first', 'lpuId': '1', 'patientId': 'demo-patient', 'esiaId': None,
            })
            assert 'подтвердил отмену' in bridge.notices['visits']['text']
            assert locate('cancelVisit').property('glyph') == 'external-link'
            click('cancelVisit')
            assert desktop_services.openUrl.call_args.args[0].toString() == 'https://gorzdrav.spb.ru/service-cancel'
            assert client.appointment_cancel.call_count == 1
        click('nav_search')
        wait_for(lambda: locate('searchAction').isVisible())
        bridge.selectSearch('working')
        _write_json(STATE_PATH, {'desktop_jobs': {
            'working': {'phase': 'attachment_denied', 'run_token': 'active'},
        }})
        bridge._refresh()
        wait_for(lambda: locate('attachmentCheck').isVisible())
        assert locate('searchStatus').property('text') == 'Запись отклонена'
        assert not locate('searchPoll').isVisible()
        feedback = locate('searchFeedback')
        check_attachment = locate('attachmentCheck')
        start_search = locate('searchAction')

        def center_y(item):
            return item.mapToScene(item.boundingRect().center()).y()

        wait_for(lambda: abs(center_y(feedback) - center_y(start_search)) < 1)
        assert abs(center_y(check_attachment) - center_y(start_search)) < 1
        assert check_attachment.height() == start_search.height()
        capture(attachment_preview, 'attachment-denied.png')
        window.setWidth(window.minimumWidth())
        wait_for(lambda: center_y(feedback) < center_y(start_search) - start_search.height() / 2)
        assert abs(center_y(check_attachment) - center_y(start_search)) < 1
        capture(attachment_preview, 'attachment-denied-compact.png')
        window.close()
        application.processEvents()
        reopened = DesktopApp(load_catalog=False)
        try:
            assert [tab['id'] for tab in reopened.view['tabs']] == ['working']
        finally:
            reopened.timer.stop()
            bridge.timer.stop()


if __name__ == '__main__':
    check_window()
