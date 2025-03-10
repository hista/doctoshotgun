#!/usr/bin/env python3
import sys
import re
import logging
import tempfile
from time import sleep
import json
from urllib.parse import urlparse
import datetime

from dateutil.parser import parse as parse_date
from dateutil.relativedelta import relativedelta

import cloudscraper
from termcolor import colored

from woob.browser.browsers import LoginBrowser
from woob.browser.url import URL
from woob.browser.pages import JsonPage, HTMLPage


def log(text, *args):
    args = [colored(arg, 'yellow') for arg in args]
    if len(args) == 1:
        text = text % args[0]
    elif len(args) > 1:
        text = text % args
    print(colored(':::', 'magenta'), text)


class Session(cloudscraper.CloudScraper):
    def send(self, *args, **kwargs):
        callback = kwargs.pop('callback', lambda future, response: response)
        is_async = kwargs.pop('is_async', False)

        if is_async:
            raise ValueError('Async requests are not supported')

        resp = super().send(*args, **kwargs)

        return callback(self, resp)


class LoginPage(JsonPage):
    pass


class CentersPage(HTMLPage):
    def iter_centers_ids(self):
        for div in self.doc.xpath('//div[@class="js-dl-search-results-calendar"]'):
            data = json.loads(div.attrib['data-props'])
            yield data['searchResultId']


class CenterResultPage(JsonPage):
    pass


class CenterPage(HTMLPage):
    pass


class CenterBookingPage(JsonPage):
    def find_motive(self, regex):
        for s in self.doc['data']['visit_motives']:
            if re.search(regex, s['name']):
                return s['id']

        return None

    def get_motives(self):
        return [s['name'] for s in self.doc['data']['visit_motives']]

    def get_places(self):
        return self.doc['data']['places']

    def get_practice(self):
        return self.doc['data']['places'][0]['practice_ids'][0]

    def get_agenda_ids(self, motive_id, practice_id=None):
        agenda_ids = []
        for a in self.doc['data']['agendas']:
            if motive_id in a['visit_motive_ids'] and \
               not a['booking_disabled'] and \
               (not practice_id or a['practice_id'] == practice_id):
                agenda_ids.append(str(a['id']))

        return agenda_ids

    def get_profile_id(self):
        return self.doc['data']['profile']['id']


class AvailabilitiesPage(JsonPage):
    def find_better_slot(self, limit=True):
        for a in self.doc['availabilities']:
            if limit and parse_date(a['date']).date() > datetime.date.today() + relativedelta(days=1):
                continue

            if len(a['slots']) == 0:
                continue
            return a['slots'][-1]


class AppointmentPage(JsonPage):
    def get_error(self):
        return self.doc['error']

    def is_error(self):
        return 'error' in self.doc


class AppointmentEditPage(JsonPage):
    def get_custom_fields(self):
        for field in self.doc['appointment']['custom_fields']:
            if field['required']:
                yield field


class AppointmentPostPage(JsonPage):
    pass


class MasterPatientPage(JsonPage):
    def get_patient(self):
        return self.doc[0]

    def get_name(self):
        return '%s %s' % (self.doc[0]['first_name'], self.doc[0]['last_name'])


class Doctolib(LoginBrowser):
    BASEURL = 'https://www.doctolib.fr'

    login = URL('/login.json', LoginPage)
    centers = URL(r'/vaccination-covid-19/(?P<where>\w+)', CentersPage)
    center_result = URL(r'/search_results/(?P<id>\d+).json', CenterResultPage)
    center = URL(r'/centre-de-sante/.*', CenterPage)
    center_booking = URL(r'/booking/(?P<center_id>.+).json', CenterBookingPage)
    availabilities = URL(r'/availabilities.json', AvailabilitiesPage)
    second_shot_availabilities = URL(r'/second_shot_availabilities.json', AvailabilitiesPage)
    appointment = URL(r'/appointments.json', AppointmentPage)
    appointment_edit = URL(r'/appointments/(?P<id>.+)/edit.json', AppointmentEditPage)
    appointment_post = URL(r'/appointments/(?P<id>.+).json', AppointmentPostPage)
    master_patient = URL(r'/account/master_patients.json', MasterPatientPage)

    def _setup_session(self, profile):
        session = Session()

        session.hooks['response'].append(self.set_normalized_url)
        if self.responses_dirname is not None:
            session.hooks['response'].append(self.save_response)

        self.session = session


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.headers['sec-fetch-dest'] = 'document'
        self.session.headers['sec-fetch-mode'] = 'navigate'
        self.session.headers['sec-fetch-site'] = 'same-origin'
        self.session.headers['User-Agent'] = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.114 Safari/537.36'

        self._logged = False

    @property
    def logged(self):
        return self._logged

    def do_login(self):
        self.open('https://www.doctolib.fr/sessions/new')
        self.login.go(json={'kind': 'patient',
                            'username': self.username,
                            'password': self.password,
                            'remember': True,
                            'remember_username': True})
        self._logged = True

    def find_centers(self, where):
        self.centers.go(where=where, params={'ref_visit_motive_ids[]': '6970', 'ref_visit_motive_ids[]': '7005'})

        for i in self.page.iter_centers_ids():
            page = self.center_result.open(id=i, params={'limit': '4', 'ref_visit_motive_ids%5B%5D': '6970', 'ref_visit_motive_ids%5B%5D': '7005', 'speciality_id': '5494', 'search_result_format': 'json'})
            # XXX return all pages even if there are no indicated availabilities.
            #for a in page.doc['availabilities']:
            #    if len(a['slots']) > 0:
            #        yield page.doc['search_result']
            yield page.doc['search_result']

    def try_to_book(self, center):
        self.open(center['url'])
        p = urlparse(center['url'])
        center_id = p.path.split('/')[-1]

        center_page = self.center_booking.go(center_id=center_id)
        profile_id = self.page.get_profile_id()
        motive_id = self.page.find_motive(r'1re.*(Pfizer|Moderna)')

        if not motive_id:
            log('Unable to find ARNm motive')
            log('Motives: %s', ', '.join(self.page.get_motives()))
            return False

        for place in self.page.get_places():
            log('Looking for slots in place %s', place['name'])
            practice_id = place['practice_ids'][0]
            agenda_ids = center_page.get_agenda_ids(motive_id, practice_id)
            if len(agenda_ids) == 0:
                # do not filter to give a chance
                agenda_ids = center_page.get_agenda_ids(motive_id)

            if self.try_to_book_place(profile_id, motive_id, practice_id, agenda_ids):
                return True

        return False

    def try_to_book_place(self, profile_id, motive_id, practice_id, agenda_ids):
        date = datetime.date.today().strftime('%Y-%m-%d')
        while date is not None:
            self.availabilities.go(params={'start_date': date,
                                           'visit_motive_ids': motive_id,
                                           'agenda_ids': '-'.join(agenda_ids),
                                           'insurance_sector': 'public',
                                           'practice_ids': practice_id,
                                           'destroy_temporary': 'true',
                                           'limit': 3})
            if 'next_slot' in self.page.doc:
                date = self.page.doc['next_slot']
            else:
                date = None

        if len(self.page.doc['availabilities']) == 0:
            log('No availabilities in this center')
            return False

        slot = self.page.find_better_slot()
        if not slot:
            log('First slot not found :(')
            return False

        log('Better slot found: %s', parse_date(slot['start_date']).strftime('%c'))

        appointment = {'profile_id':    profile_id,
                       'source_action': 'profile',
                       'start_date':    slot['start_date'],
                       'visit_motive_ids': str(motive_id),
                      }

        data = {'agenda_ids': '-'.join(agenda_ids),
                'appointment': appointment,
                'practice_ids': [practice_id]}

        headers = {
                   'content-type': 'application/json',
                  }
        self.appointment.go(data=json.dumps(data), headers=headers)

        if self.page.is_error():
            log('Appointment not available anymore :( %s', self.page.get_error())
            return False

        self.second_shot_availabilities.go(params={'start_date': slot['steps'][1]['start_date'].split('T')[0],
                                                   'visit_motive_ids': motive_id,
                                                   'agenda_ids': '-'.join(agenda_ids),
                                                   'first_slot': slot['start_date'],
                                                   'insurance_sector': 'public',
                                                   'practice_ids': practice_id,
                                                   'limit': 3})

        second_slot = self.page.find_better_slot(limit=False)
        if not second_slot:
            log('No second shot found')
            return False

        log('Second shot: %s', parse_date(second_slot['start_date']).strftime('%c'))

        data['second_slot'] = second_slot['start_date']
        self.appointment.go(data=json.dumps(data), headers=headers)

        if self.page.is_error():
            log('Appointment not available anymore :( %s', self.page.get_error())
            return False

        a_id = self.page.doc['id']

        self.appointment_edit.go(id=a_id)
        self.master_patient.go()

        master_patient = self.page.get_patient()

        log('Booking for %s...', self.page.get_name())

        self.appointment_edit.go(id=a_id, params={'master_patient_id': master_patient['id']})

        custom_fields = {}
        for field in self.page.get_custom_fields():
            if field['id'] == 'cov19':
                value = 'Non'
            elif field['placeholder']:
                value = field['placeholder']
            else:
                print('%s (%s):' % (field['label'], field['placeholder']), end=' ', flush=True)
                value = sys.stdin.readline().strip()

            custom_fields[field['id']] = value

        data = {'appointment': {'custom_fields_values': custom_fields,
                                'new_patient': True,
                                'qualification_answers': {},
                                'referrer_id': None,
                               },
                'bypass_mandatory_relative_contact_info': False,
                'email': None,
                'master_patient': master_patient,
                'new_patient': True,
                'patient': None,
                'phone_number': None,
               }

        self.appointment_post.go(id=a_id, data=json.dumps(data), headers=headers, method='PUT')

        if 'redirection' in self.page.doc:
            log('Go on %s', 'https://www.doctolib.fr' + self.page.doc['redirection'])

        self.appointment_post.go(id=a_id)

        log('Booking status: %s', self.page.doc['confirmed'])

        return True

class Application:
    def main(self, argv):
        logging.basicConfig(level=logging.DEBUG)
        responses_dirname = tempfile.mkdtemp(prefix='woob_session_')

        if len(argv) < 3:
            print('Usage: %s CITY USERNAME [PASSWORD]' % argv[0])
            return 1

        city = argv[1]
        username = argv[2]

        if len(argv) < 4:
            print('Password:', end=' ', flush=True)
            password = sys.stdin.readline().strip()
        else:
            password = argv[3]

        docto = Doctolib(username, password, responses_dirname=responses_dirname)

        while True:
            for center in docto.find_centers(city):
                log('Trying to find a slot in %s', center['name_with_title'])

                if not docto.logged:
                    docto.do_login()

                if docto.try_to_book(center):
                    log('Booked!')
                    return 0

                log('Fail, try next center...')
                sleep(1)

            sleep(5)

        return 0


if __name__ == '__main__':
    try:
        sys.exit(Application().main(sys.argv))
    except KeyboardInterrupt:
        print('Abort.')
        sys.exit(1)
