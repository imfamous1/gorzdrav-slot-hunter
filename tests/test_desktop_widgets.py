import unittest

from gorzdrav_hunter.desktop_catalog import match_choices


class SearchChoicesTests(unittest.TestCase):
    def test_search_matches_parts_of_name_address_and_number(self):
        records = [
            {'id': 1, 'name': 'Поликлиника №52',
             'label': 'Поликлиника №52 — улица Асафьева'},
            {'id': 2, 'name': 'Детская поликлиника №52',
             'label': 'Детская поликлиника №52 — проспект Просвещения'},
        ]
        self.assertEqual([records[0]], match_choices(records, '№52 АСАФ'))
        self.assertEqual([], match_choices(records, 'невский'))
        self.assertEqual(records, match_choices(records, '  '))

    def test_search_treats_yo_and_e_as_equal(self):
        records = [{'name': 'Женская консультация на Артёма'}]
        self.assertEqual(records, match_choices(records, 'АРТЕМА'))


if __name__ == '__main__':
    unittest.main()
