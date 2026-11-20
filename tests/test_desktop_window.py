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
    from unittest.mock import patch

    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtTest import QTest

    from gorzdrav_hunter.config import CONFIG_PATH, STATE_PATH, _write_json
    from gorzdrav_hunter.desktop import DesktopApp, create_window
    from gorzdrav_hunter.desktop_plan import apply_plan

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
        'target': {**target, 'reschedule_from': {
            'appointment_id': 'example', 'specialty_id': 'eye', 'visit_start': visit,
        }},
        'patient': 'patient', 'consent': True, 'run': {'worker': 'fixture', 'token': 'active'},
    }
    stopped = {'target': {**target, 'auto_book': False}, 'enabled': False}
    _write_json(CONFIG_PATH, apply_plan({}, {
        'patients': {'patient': {'last_name': 'Примерова', 'first_name': 'Анна'}},
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

        wait_for(lambda: window.isExposed() and locate('searchTab_working') is not None)
        rail = locate('navigationRail')
        for name in ('navigationBrand', 'nav_search', 'nav_patient', 'nav_telegram', 'nav_history'):
            item = locate(name)
            x = rail.mapFromScene(item.mapToScene(QPointF())).x()
            right = rail.width() - x - item.width()
            assert abs(16 - x) < 0.5 and abs(x - right) < 0.5, (name, x, right)
        assert tab_names() == ['working']
        assert locate('searchAction').property('text') == 'Остановить поиск'

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
        bridge.setSearch('start', '05.12.2031')
        click('searchTab_working')
        wait_for(lambda: bridge.selected == 'working')
        assert locate('searchAction').property('text') == 'Остановить поиск'
        click('searchTab_' + draft)
        wait_for(lambda: bridge.selected == draft)
        assert bridge.view['start'] == '05.12.2031'

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
        toggle = locate('telegramSwitch')
        assert toggle.property('text').endswith('выключены')
        click('telegramSwitch')
        assert bridge.view['telegram']['enabled']
        assert toggle.property('text').endswith('включены')
        click('telegramSwitch')
        assert not bridge.view['telegram']['enabled']
        assert toggle.property('text').endswith('выключены')

        click('nav_history')
        wait_for(lambda: not locate('searchAction').isVisible())
        click('nav_search')
        wait_for(lambda: locate('searchAction').isVisible())
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
