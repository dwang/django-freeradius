import csv
import os
from base64 import encodestring
from hashlib import md5, sha1
from io import StringIO
from os import urandom

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import models
from django.db.models import Count, ProtectedError
from django.utils.crypto import get_random_string
from django.utils.encoding import python_2_unicode_compatible
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _
from openwisp_utils.base import TimeStampedEditableModel
from passlib.hash import lmhash, nthash, sha512_crypt

from django_freeradius.settings import (
    BATCH_DEFAULT_PASSWORD_LENGTH, BATCH_MAIL_MESSAGE, BATCH_MAIL_SENDER, BATCH_MAIL_SUBJECT,
)
from django_freeradius.utils import (
    find_available_username, generate_pdf, prefix_generate_users, validate_csvfile,
)

from .. import settings as app_settings

RADOP_CHECK_TYPES = (('=', '='),
                     (':=', ':='),
                     ('==', '=='),
                     ('+=', '+='),
                     ('!=', '!='),
                     ('>', '>'),
                     ('>=', '>='),
                     ('<', '<'),
                     ('<=', '<='),
                     ('=~', '=~'),
                     ('!~', '!~'),
                     ('=*', '=*'),
                     ('!*', '!*'))

RAD_NAS_TYPES = (
    ('Async', 'Async'),
    ('Sync', 'Sync'),
    ('ISDN Sync', 'ISDN Sync'),
    ('ISDN Async V.120', 'ISDN Async V.120'),
    ('ISDN Async V.110', 'ISDN Async V.110'),
    ('Virtual', 'Virtual'),
    ('PIAFS', 'PIAFS'),
    ('HDLC Clear', 'HDLC Clear'),
    ('Channel', 'Channel'),
    ('X.25', 'X.25'),
    ('X.75', 'X.75'),
    ('G.3 Fax', 'G.3 Fax'),
    ('SDSL', 'SDSL - Symmetric DSL'),
    ('ADSL-CAP', 'ADSL-CAP'),
    ('ADSL-DMT', 'ADSL-DMT'),
    ('IDSL', 'IDSL'),
    ('Ethernet', 'Ethernet'),
    ('xDSL', 'xDSL'),
    ('Cable', 'Cable'),
    ('Wireless - Other', 'Wireless - Other'),
    ('IEEE 802.11', 'Wireless - IEEE 802.11'),
    ('Token-Ring', 'Token-Ring'),
    ('FDDI', 'FDDI'),
    ('Wireless - CDMA2000', 'Wireless - CDMA2000'),
    ('Wireless - UMTS', 'Wireless - UMTS'),
    ('Wireless - 1X-EV', 'Wireless - 1X-EV'),
    ('IAPP', 'IAPP'),
    ('FTTP', 'FTTP'),
    ('IEEE 802.16', 'Wireless - IEEE 802.16'),
    ('IEEE 802.20', 'Wireless - IEEE 802.20'),
    ('IEEE 802.22', 'Wireless - IEEE 802.22'),
    ('PPPoA', 'PPPoA - PPP over ATM'),
    ('PPPoEoA', 'PPPoEoA - PPP over Ethernet over ATM'),
    ('PPPoEoE', 'PPPoEoE - PPP over Ethernet over Ethernet'),
    ('PPPoEoVLAN', 'PPPoEoVLAN - PPP over Ethernet over VLAN'),
    ('PPPoEoQinQ', 'PPPoEoQinQ - PPP over Ethernet over IEEE 802.1QinQ'),
    ('xPON', 'xPON - Passive Optical Network'),
    ('Wireless - XGP', 'Wireless - XGP'),
    ('WiMAX', ' WiMAX Pre-Release 8 IWK Function'),
    ('WIMAX-WIFI-IWK', 'WIMAX-WIFI-IWK: WiMAX WIFI Interworking'),
    ('WIMAX-SFF', 'WIMAX-SFF: Signaling Forwarding Function for LTE/3GPP2'),
    ('WIMAX-HA-LMA', 'WIMAX-HA-LMA: WiMAX HA and or LMA function'),
    ('WIMAX-DHCP', 'WIMAX-DHCP: WIMAX DCHP service'),
    ('WIMAX-LBS', 'WIMAX-LBS: WiMAX location based service'),
    ('WIMAX-WVS', 'WIMAX-WVS: WiMAX voice service'),
    ('Other', 'Other'),
)


RADOP_REPLY_TYPES = (('=', '='),
                     (':=', ':='),
                     ('+=', '+='))

RADCHECK_ATTRIBUTE_TYPES = ['Max-Daily-Session',
                            'Max-All-Session',
                            'Max-Daily-Session-Traffic']

RADCHECK_PASSWD_TYPE = ['Cleartext-Password',
                        'NT-Password',
                        'LM-Password',
                        'MD5-Password',
                        'SMD5-Password',
                        'SHA-Password',
                        'SSHA-Password',
                        'Crypt-Password']

RADCHECK_ATTRIBUTE_TYPES += RADCHECK_PASSWD_TYPE

STRATEGIES = (
    ('prefix', _('Generate from prefix')),
    ('csv', _('Import from CSV'))
)


class BaseModel(TimeStampedEditableModel):
    id = None

    class Meta:
        abstract = True


_NOT_BLANK_MESSAGE = _('This field cannot be blank.')


class AutoUsernameMixin(object):
    def clean(self):
        """
        automatically sets username
        """
        if self.user:
            self.username = self.user.username
        elif not self.username:
            raise ValidationError({
                'username': _NOT_BLANK_MESSAGE,
                'user': _NOT_BLANK_MESSAGE
            })


class AutoGroupnameMixin(object):
    def clean(self):
        """
        automatically sets groupname
        """
        super().clean()
        if self.group:
            self.groupname = self.group.name
        elif not self.groupname:
            raise ValidationError({
                'groupname': _NOT_BLANK_MESSAGE,
                'group': _NOT_BLANK_MESSAGE
            })


class AbstractRadiusCheckQueryset(models.query.QuerySet):
    def filter_duplicate_username(self):
        pks = []
        for i in self.values('username').annotate(Count('id')).order_by().filter(id__count__gt=1):
            pks.extend([account.pk for account in self.filter(username=i['username'])])
        return self.filter(pk__in=pks)

    def filter_duplicate_value(self):
        pks = []
        for i in self.values('value').annotate(Count('id')).order_by().filter(id__count__gt=1):
            pks.extend([accounts.pk for accounts in self.filter(value=i['value'])])
        return self.filter(pk__in=pks)

    def filter_expired(self):
        return self.filter(valid_until__lt=now())

    def filter_not_expired(self):
        return self.filter(valid_until__gte=now())


def _encode_secret(attribute, new_value=None):
    if attribute == 'NT-Password':
        attribute_value = nthash.hash(new_value)
    elif attribute == 'LM-Password':
        attribute_value = lmhash.hash(new_value)
    elif attribute == 'MD5-Password':
        attribute_value = md5(new_value.encode('utf-8')).hexdigest()
    elif attribute == 'SMD5-Password':
        salt = urandom(4)
        hash = md5(new_value.encode('utf-8'))
        hash.update(salt)
        hash_encoded = encodestring(hash.digest() + salt)
        attribute_value = hash_encoded.decode('utf-8')[:-1]
    elif attribute == 'SHA-Password':
        attribute_value = sha1(new_value.encode('utf-8')).hexdigest()
    elif attribute == 'SSHA-Password':
        salt = urandom(4)
        hash = sha1(new_value.encode('utf-8'))
        hash.update(salt)
        hash_encoded = encodestring(hash.digest() + salt)
        attribute_value = hash_encoded.decode('utf-8')[:-1]
    elif attribute == 'Crypt-Password':
        attribute_value = sha512_crypt.hash(new_value)
    else:
        attribute_value = new_value
    return attribute_value


class AbstractRadiusCheckManager(models.Manager):
    def get_queryset(self):
        return AbstractRadiusCheckQueryset(self.model, using=self._db)

    def create(self, *args, **kwargs):
        if 'new_value' in kwargs:
            kwargs['value'] = _encode_secret(kwargs['attribute'],
                                             kwargs['new_value'])
            del(kwargs['new_value'])
        return super(AbstractRadiusCheckManager, self).create(*args, **kwargs)


@python_2_unicode_compatible
class AbstractRadiusCheck(AutoUsernameMixin, BaseModel):
    username = models.CharField(verbose_name=_('username'),
                                max_length=64,
                                db_index=True,
                                # blank values are forbidden with custom validation
                                # because this field can left blank if the user
                                # foreign key is filled (it will be auto-filled)
                                blank=True)
    value = models.CharField(verbose_name=_('value'), max_length=253)
    op = models.CharField(verbose_name=_('operator'),
                          max_length=2,
                          choices=RADOP_CHECK_TYPES,
                          default=':=')
    attribute = models.CharField(verbose_name=_('attribute'),
                                 max_length=64,
                                 choices=[(i, i) for i in RADCHECK_ATTRIBUTE_TYPES
                                          if i not in
                                          app_settings.DISABLED_SECRET_FORMATS],
                                 default=app_settings.DEFAULT_SECRET_FORMAT)
    # the foreign key is not part of the standard freeradius schema
    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE,
                             blank=True,
                             null=True)
    # additional fields to enable more granular checks
    is_active = models.BooleanField(default=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    # internal notes
    notes = models.TextField(null=True, blank=True)
    # custom manager
    objects = AbstractRadiusCheckManager()

    class Meta:
        db_table = 'radcheck'
        verbose_name = _('check')
        verbose_name_plural = _('checks')
        abstract = True

    def __str__(self):
        return self.username


@python_2_unicode_compatible
class AbstractRadiusReply(AutoUsernameMixin, BaseModel):
    username = models.CharField(verbose_name=_('username'),
                                max_length=64,
                                db_index=True,
                                # blank values are forbidden with custom validation
                                # because this field can left blank if the user
                                # foreign key is filled (it will be auto-filled)
                                blank=True)
    value = models.CharField(verbose_name=_('value'), max_length=253)
    op = models.CharField(verbose_name=_('operator'),
                          max_length=2,
                          choices=RADOP_REPLY_TYPES,
                          default='=')
    attribute = models.CharField(verbose_name=_('attribute'), max_length=64)
    # the foreign key is not part of the standard freeradius schema
    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE,
                             blank=True,
                             null=True)

    class Meta:
        db_table = 'radreply'
        verbose_name = _('reply')
        verbose_name_plural = _('replies')
        abstract = True

    def __str__(self):
        return self.username


@python_2_unicode_compatible
class AbstractRadiusAccounting(models.Model):
    id = models.BigAutoField(primary_key=True, db_column='radacctid')
    session_id = models.CharField(verbose_name=_('session ID'),
                                  max_length=64,
                                  db_column='acctsessionid',
                                  db_index=True)
    unique_id = models.CharField(verbose_name=_('accounting unique ID'),
                                 max_length=32,
                                 db_column='acctuniqueid',
                                 unique=True)
    username = models.CharField(verbose_name=_('username'),
                                max_length=64,
                                db_index=True,
                                null=True,
                                blank=True)
    groupname = models.CharField(verbose_name=_('group name'),
                                 max_length=64,
                                 null=True,
                                 blank=True)
    realm = models.CharField(verbose_name=_('realm'),
                             max_length=64,
                             null=True,
                             blank=True)
    nas_ip_address = models.GenericIPAddressField(verbose_name=_('NAS IP address'),
                                                  db_column='nasipaddress',
                                                  db_index=True)
    nas_port_id = models.CharField(verbose_name=_('NAS port ID'),
                                   max_length=15,
                                   db_column='nasportid',
                                   null=True,
                                   blank=True)
    nas_port_type = models.CharField(verbose_name=_('NAS port type'),
                                     max_length=32,
                                     db_column='nasporttype',
                                     null=True,
                                     blank=True)
    start_time = models.DateTimeField(verbose_name=_('start time'),
                                      db_column='acctstarttime',
                                      db_index=True,
                                      null=True,
                                      blank=True)
    update_time = models.DateTimeField(verbose_name=_('update time'),
                                       db_column='acctupdatetime',
                                       null=True,
                                       blank=True)
    stop_time = models.DateTimeField(verbose_name=_('stop time'),
                                     db_column='acctstoptime',
                                     db_index=True,
                                     null=True,
                                     blank=True)
    interval = models.IntegerField(verbose_name=_('interval'),
                                   db_column='acctinterval',
                                   null=True,
                                   blank=True)
    session_time = models.PositiveIntegerField(verbose_name=_('session time'),
                                               db_column='acctsessiontime',
                                               null=True,
                                               blank=True)
    authentication = models.CharField(verbose_name=_('authentication'),
                                      max_length=32,
                                      db_column='acctauthentic',
                                      null=True,
                                      blank=True)
    connection_info_start = models.CharField(verbose_name=_('connection info start'),
                                             max_length=50,
                                             db_column='connectinfo_start',
                                             null=True,
                                             blank=True)
    connection_info_stop = models.CharField(verbose_name=_('connection info stop'),
                                            max_length=50,
                                            db_column='connectinfo_stop',
                                            null=True,
                                            blank=True)
    input_octets = models.BigIntegerField(verbose_name=_('input octets'),
                                          db_column='acctinputoctets',
                                          null=True,
                                          blank=True)
    output_octets = models.BigIntegerField(verbose_name=_('output octets'),
                                           db_column='acctoutputoctets',
                                           null=True,
                                           blank=True)
    called_station_id = models.CharField(verbose_name=_('called station ID'),
                                         max_length=50,
                                         db_column='calledstationid',
                                         db_index=True,
                                         blank=True,
                                         null=True)
    calling_station_id = models.CharField(verbose_name=_('calling station ID'),
                                          max_length=50,
                                          db_column='callingstationid',
                                          db_index=True,
                                          blank=True,
                                          null=True)
    terminate_cause = models.CharField(verbose_name=_('termination cause'),
                                       max_length=32,
                                       db_column='acctterminatecause',
                                       blank=True,
                                       null=True)
    service_type = models.CharField(verbose_name=_('service type'),
                                    max_length=32,
                                    db_column='servicetype',
                                    null=True,
                                    blank=True)
    framed_protocol = models.CharField(verbose_name=_('framed protocol'),
                                       max_length=32,
                                       db_column='framedprotocol',
                                       null=True,
                                       blank=True)
    framed_ip_address = models.GenericIPAddressField(verbose_name=_('framed IP address'),
                                                     db_column='framedipaddress',
                                                     db_index=True,
                                                     # the default MySQL freeradius schema defines
                                                     # this as NOT NULL but defaulting to empty string
                                                     # but that wouldn't work on PostgreSQL
                                                     null=True,
                                                     blank=True)

    def save(self, *args, **kwargs):
        if not self.start_time:
            self.start_time = now()
        super(AbstractRadiusAccounting, self).save(*args, **kwargs)

    class Meta:
        db_table = 'radacct'
        verbose_name = _('accounting')
        verbose_name_plural = _('accountings')
        abstract = True

    def __str__(self):
        return self.unique_id


@python_2_unicode_compatible
class AbstractNas(BaseModel):
    name = models.CharField(verbose_name=_('name'),
                            max_length=128,
                            help_text=_('NAS Name (or IP address)'),
                            db_index=True,
                            db_column='nasname')
    short_name = models.CharField(verbose_name=_('short name'),
                                  max_length=32,
                                  db_column='shortname')
    type = models.CharField(verbose_name=_('type'),
                            max_length=30,
                            default='other')
    ports = models.PositiveIntegerField(verbose_name=_('ports'),
                                        blank=True,
                                        null=True)
    secret = models.CharField(verbose_name=_('secret'),
                              max_length=60,
                              help_text=_('Shared Secret'))
    server = models.CharField(verbose_name=_('server'),
                              max_length=64,
                              blank=True,
                              null=True)
    community = models.CharField(verbose_name=_('community'),
                                 max_length=50,
                                 blank=True,
                                 null=True)
    description = models.CharField(verbose_name=_('description'),
                                   max_length=200,
                                   null=True,
                                   blank=True)

    class Meta:
        db_table = 'nas'
        verbose_name = _('NAS')
        verbose_name_plural = _('NAS')
        abstract = True

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class AbstractRadiusGroup(BaseModel):
    """
    This is not part of the standard freeradius schema.
    It's added to facilitate the management of groups.
    """
    id = TimeStampedEditableModel._meta.get_field('id')
    name = models.CharField(verbose_name=_('group name'),
                            max_length=255,
                            unique=True,
                            db_index=True)
    description = models.CharField(verbose_name=_('description'),
                                   max_length=64,
                                   blank=True,
                                   null=True)
    _DEFAULT_HELP_TEXT = (
        'The default group is automatically assigned to new users; '
        'changing the default group has only effect on new users '
        '(existing users will keep being members of their current group)'
    )
    default = models.BooleanField(verbose_name=_('is default?'),
                                  help_text=_(_DEFAULT_HELP_TEXT),
                                  default=False)

    class Meta:
        verbose_name = _('group')
        verbose_name_plural = _('groups')
        abstract = True

    def __str__(self):
        return self.name

    def clean(self):
        if not self.default:
            self.check_default()

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        if self.default:
            self.set_default()
        # sync all related records
        if not self._state.adding:
            self.radiusgroupcheck_set.update(groupname=self.name)
            self.radiusgroupreply_set.update(groupname=self.name)
            self.radiususergroup_set.update(groupname=self.name)
        return result

    _DEFAULT_VALIDATION_ERROR = _(
        'There must be at least one default group present in '
        'the system. To change the default group, simply set '
        'as deafult the group you want to make the new deafult.'
    )
    _DEFAULT_PROTECTED_ERROR = _('The default group cannot be deleted')

    def delete(self, *args, **kwargs):
        if self.default:
            raise ProtectedError(self._DEFAULT_PROTECTED_ERROR, self)
        return super().delete(*args, **kwargs)

    def set_default(self):
        """
        ensures there's only 1 default group
        (logic overridable via custom models)
        """
        queryset = self.get_default_queryset()
        if queryset.exists():
            queryset.update(default=False)

    def check_default(self):
        """
        ensures the default group cannot be undefaulted
        (logic overridable via custom models)
        """
        if not self.get_default_queryset().exists():
            raise ValidationError(
                {'default': self._DEFAULT_VALIDATION_ERROR}
            )

    def get_default_queryset(self):
        """
        looks for default groups excluding the current one
        overridable by openwisp-radius and other 3rd party apps
        """
        return self.__class__.objects.exclude(pk=self.pk) \
                                     .filter(default=True)


@python_2_unicode_compatible
class AbstractRadiusUserGroup(AutoGroupnameMixin, AutoUsernameMixin,
                              BaseModel):
    username = models.CharField(verbose_name=_('username'),
                                max_length=64,
                                db_index=True,
                                # blank values are forbidden with custom validation
                                # because this field can left blank if the user
                                # foreign key is filled (it will be auto-filled)
                                blank=True)
    groupname = models.CharField(verbose_name=_('group name'),
                                 max_length=64,
                                 # blank values are forbidden with custom validation
                                 # because this field can left blank if the group
                                 # foreign key is filled (it will be auto-filled)
                                 blank=True)
    priority = models.IntegerField(verbose_name=_('priority'), default=1)
    # the foreign keys are not part of the standard freeradius schema,
    # these are added here to facilitate the synchronization of the
    # records which are related in different tables
    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE,
                             blank=True,
                             null=True)
    group = models.ForeignKey('RadiusGroup',
                              on_delete=models.CASCADE,
                              blank=True,
                              null=True)

    class Meta:
        db_table = 'radusergroup'
        verbose_name = _('user group')
        verbose_name_plural = _('user groups')
        abstract = True

    def __str__(self):
        return str(self.username)


@python_2_unicode_compatible
class AbstractRadiusGroupCheck(AutoGroupnameMixin, BaseModel):
    groupname = models.CharField(verbose_name=_('group name'),
                                 max_length=64,
                                 db_index=True,
                                 # blank values are forbidden with custom validation
                                 # because this field can left blank if the group
                                 # foreign key is filled (it will be auto-filled)
                                 blank=True)
    attribute = models.CharField(verbose_name=_('attribute'), max_length=64)
    op = models.CharField(verbose_name=_('operator'),
                          max_length=2,
                          choices=RADOP_CHECK_TYPES,
                          default=':=')
    value = models.CharField(verbose_name=_('value'), max_length=253)
    # the foreign key is not part of the standard freeradius schema
    group = models.ForeignKey('RadiusGroup',
                              on_delete=models.CASCADE,
                              blank=True,
                              null=True)

    class Meta:
        db_table = 'radgroupcheck'
        verbose_name = _('group check')
        verbose_name_plural = _('group checks')
        abstract = True

    def __str__(self):
        return str(self.groupname)


@python_2_unicode_compatible
class AbstractRadiusGroupReply(AutoGroupnameMixin, BaseModel):
    groupname = models.CharField(verbose_name=_('group name'),
                                 max_length=64,
                                 db_index=True,
                                 # blank values are forbidden with custom validation
                                 # because this field can left blank if the group
                                 # foreign key is filled (it will be auto-filled)
                                 blank=True)
    attribute = models.CharField(verbose_name=_('attribute'), max_length=64)
    op = models.CharField(verbose_name=_('operator'),
                          max_length=2,
                          choices=RADOP_REPLY_TYPES,
                          default='=')
    value = models.CharField(verbose_name=_('value'), max_length=253)
    # the foreign key is not part of the standard freeradius schema
    group = models.ForeignKey('RadiusGroup',
                              on_delete=models.CASCADE,
                              blank=True,
                              null=True)

    class Meta:
        db_table = 'radgroupreply'
        verbose_name = _('group reply')
        verbose_name_plural = _('group replies')
        abstract = True

    def __str__(self):
        return str(self.groupname)


@python_2_unicode_compatible
class AbstractRadiusPostAuth(models.Model):
    username = models.CharField(verbose_name=_('username'),
                                max_length=64)
    password = models.CharField(verbose_name=_('password'),
                                max_length=64,
                                db_column='pass',
                                blank=True)
    reply = models.CharField(verbose_name=_('reply'),
                             max_length=32)
    called_station_id = models.CharField(verbose_name=_('called station ID'),
                                         max_length=50,
                                         db_column='calledstationid',
                                         blank=True,
                                         null=True)
    calling_station_id = models.CharField(verbose_name=_('calling station ID'),
                                          max_length=50,
                                          db_column='callingstationid',
                                          blank=True,
                                          null=True)
    date = models.DateTimeField(verbose_name=_('date'),
                                db_column='authdate',
                                auto_now_add=True)

    class Meta:
        db_table = 'radpostauth'
        verbose_name = _('post auth')
        verbose_name_plural = _('post auth log')
        abstract = True

    def __str__(self):
        return str(self.username)


class AbstractRadiusBatch(TimeStampedEditableModel):
    strategy = models.CharField(_('strategy'),
                                max_length=16,
                                choices=STRATEGIES,
                                db_index=True,
                                help_text=_('Import users from a CSV or generate using a prefix'))
    name = models.CharField(verbose_name=_('name'),
                            max_length=128,
                            help_text=_('A unique batch name'),
                            db_index=True,
                            unique=True)
    users = models.ManyToManyField(settings.AUTH_USER_MODEL,
                                   blank=True,
                                   related_name='radius_batch',
                                   help_text=_('List of users uploaded in this batch'))
    csvfile = models.FileField(null=True,
                               blank=True,
                               verbose_name='CSV',
                               help_text=_('The csv file containing the user details to be uploaded'))
    prefix = models.CharField(_('prefix'),
                              null=True,
                              blank=True,
                              max_length=20,
                              help_text=_('Usernames generated will be of the format [prefix][number]'))
    pdf = models.FileField(null=True,
                           blank=True,
                           verbose_name='PDF',
                           help_text=_('The pdf file containing list of usernames and passwords'))
    expiration_date = models.DateField(verbose_name=_('expiration date'),
                                       null=True,
                                       blank=True,
                                       help_text=_('If left blank users will never expire'))

    class Meta:
        db_table = 'radbatch'
        verbose_name = _('batch user creation')
        verbose_name_plural = _('batch user creation operations')
        abstract = True

    def __str__(self):
        return self.name

    def clean(self):
        if self.strategy == 'csv' and not self.csvfile:
            raise ValidationError({'csvfile': _('This field cannot be blank.')},
                                  code='invalid')
        if self.strategy == 'prefix' and not self.prefix:
            raise ValidationError({'prefix': _('This field cannot be blank.')},
                                  code='invalid')
        if self.strategy == 'csv' and self.prefix or self.strategy == 'prefix' and self.csvfile:
            # this case would happen only when using the internal API
            raise ValidationError(
                _('Mixing fields of different strategies'),
                code='invalid',
            )
        if self.strategy == 'csv':
            validate_csvfile(self.csvfile.file)
        super(AbstractRadiusBatch, self).clean()

    def add(self, reader, password_length=BATCH_DEFAULT_PASSWORD_LENGTH):
        User = get_user_model()
        users_list = []
        generated_passwords = []
        for row in reader:
            if len(row) == 5:
                username, password, email, first_name, last_name = row
                if not username and email:
                    username = email.split('@')[0]
                username = find_available_username(username, users_list)
                user = User(username=username,
                            email=email,
                            first_name=first_name,
                            last_name=last_name)
                cleartext_delimiter = 'cleartext$'
                if not password:
                    password = get_random_string(length=password_length)
                    user.set_password(password)
                    generated_passwords.append([username, password, email])
                elif password and password.startswith(cleartext_delimiter):
                    password = password[len(cleartext_delimiter):]
                    user.set_password(password)
                else:
                    user.password = password
                user.full_clean()
                users_list.append(user)
        for user in users_list:
            self.save_user(user)
        for element in generated_passwords:
            username, password, user_email = element
            send_mail(
                BATCH_MAIL_SUBJECT,
                BATCH_MAIL_MESSAGE.format(username, password),
                BATCH_MAIL_SENDER,
                [user_email]
            )

    def csvfile_upload(self, csvfile, password_length=BATCH_DEFAULT_PASSWORD_LENGTH):
        csv_data = csvfile.read()
        csv_data = csv_data.decode('utf-8') if isinstance(csv_data, bytes) else csv_data
        reader = csv.reader(StringIO(csv_data), delimiter=',')
        self.full_clean()
        self.save()
        self.add(reader, password_length)

    def prefix_add(self, prefix, n, password_length=BATCH_DEFAULT_PASSWORD_LENGTH):
        self.save()
        users_list, user_password = prefix_generate_users(prefix, n, password_length)
        for user in users_list:
            user.full_clean()
            self.save_user(user)
        pdf_file = generate_pdf(prefix, {'users': user_password})
        pdf_file.name = pdf_file.name.split('/')[-1]
        self.pdf = pdf_file
        self.full_clean()
        self.save()

    def save_user(self, user):
        user.save()
        self.users.add(user)

    def delete(self):
        self.users.all().delete()
        super().delete()
        self._remove_files()

    def expire(self):
        users = self.users.all()
        for u in users:
            u.is_active = False
            u.save()

    def _remove_files(self):
        strategy_filemap = {
            'prefix': 'pdf',
            'csv': 'csvfile'
        }
        path = getattr(self, strategy_filemap.get(self.strategy)).path
        if os.path.isfile(path):
            os.remove(path)
