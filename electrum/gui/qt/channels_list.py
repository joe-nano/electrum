# -*- coding: utf-8 -*-
import traceback
from enum import IntEnum
from typing import Sequence, Optional

from PyQt5 import QtCore, QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QMenu, QHBoxLayout, QLabel, QVBoxLayout, QGridLayout, QLineEdit,
                             QPushButton, QAbstractItemView)
from PyQt5.QtGui import QFont, QStandardItem, QBrush

from electrum.util import bh2u, NotEnoughFunds, NoDynamicFeeEstimates
from electrum.i18n import _
from electrum.lnchannel import Channel, peer_states
from electrum.wallet import Abstract_Wallet
from electrum.lnutil import LOCAL, REMOTE, format_short_channel_id, LN_MAX_FUNDING_SAT
from electrum.lnworker import LNWallet

from .util import (MyTreeView, WindowModalDialog, Buttons, OkButton, CancelButton,
                   EnterButton, WaitingDialog, MONOSPACE_FONT, ColorScheme)
from .amountedit import BTCAmountEdit, FreezableLineEdit


ROLE_CHANNEL_ID = Qt.UserRole


class ChannelsList(MyTreeView):
    update_rows = QtCore.pyqtSignal(Abstract_Wallet)
    update_single_row = QtCore.pyqtSignal(Channel)

    class Columns(IntEnum):
        SHORT_CHANID = 0
        NODE_ID = 1
        NODE_ALIAS = 2
        LOCAL_BALANCE = 3
        REMOTE_BALANCE = 4
        CHANNEL_STATUS = 5

    headers = {
        Columns.SHORT_CHANID: _('Short Channel ID'),
        Columns.NODE_ID: _('Node ID'),
        Columns.NODE_ALIAS: _('Node alias'),
        Columns.LOCAL_BALANCE: _('Local'),
        Columns.REMOTE_BALANCE: _('Remote'),
        Columns.CHANNEL_STATUS: _('Status'),
    }

    _default_item_bg_brush = None  # type: Optional[QBrush]

    def __init__(self, parent):
        super().__init__(parent, self.create_menu, stretch_column=self.Columns.NODE_ID,
                         editable_columns=[])
        self.setModel(QtGui.QStandardItemModel(self))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.main_window = parent
        self.update_rows.connect(self.do_update_rows)
        self.update_single_row.connect(self.do_update_single_row)
        self.network = self.parent.network
        self.lnworker = self.parent.wallet.lnworker
        self.lnbackups = self.parent.wallet.lnbackups
        self.setSortingEnabled(True)

    def format_fields(self, chan):
        labels = {}
        for subject in (REMOTE, LOCAL):
            bal_minus_htlcs = chan.balance_minus_outgoing_htlcs(subject)//1000
            label = self.parent.format_amount(bal_minus_htlcs)
            other = subject.inverted()
            bal_other = chan.balance(other)//1000
            bal_minus_htlcs_other = chan.balance_minus_outgoing_htlcs(other)//1000
            if bal_other != bal_minus_htlcs_other:
                label += ' (+' + self.parent.format_amount(bal_other - bal_minus_htlcs_other) + ')'
            labels[subject] = label
        status = chan.get_state_for_GUI()
        closed = chan.is_closed()
        if self.parent.network.is_lightning_running():
            node_info = self.lnworker.channel_db.get_node_info_for_node_id(chan.node_id)
            node_alias = (node_info.alias if node_info else '') or ''
        else:
            node_alias = ''
        return [
            chan.short_id_for_GUI(),
            bh2u(chan.node_id),
            node_alias,
            '' if closed else labels[LOCAL],
            '' if closed else labels[REMOTE],
            status
        ]

    def on_success(self, txid):
        self.main_window.show_error('Channel closed' + '\n' + txid)

    def on_failure(self, exc_info):
        type_, e, tb = exc_info
        traceback.print_tb(tb)
        self.main_window.show_error('Failed to close channel:\n{}'.format(repr(e)))

    def close_channel(self, channel_id):
        msg = _('Close channel?')
        if not self.parent.question(msg):
            return
        def task():
            coro = self.lnworker.close_channel(channel_id)
            return self.network.run_from_another_thread(coro)
        WaitingDialog(self, 'please wait..', task, self.on_success, self.on_failure)

    def force_close(self, channel_id):
        chan = self.lnworker.channels[channel_id]
        to_self_delay = chan.config[REMOTE].to_self_delay
        msg = _('Force-close channel?') + '\n\n'\
              + _('Funds retrieved from this channel will not be available before {} blocks after forced closure.').format(to_self_delay) + ' '\
              + _('After that delay, funds will be sent to an address derived from your wallet seed.') + '\n\n'\
              + _('In the meantime, channel funds will not be recoverable from your seed, and might be lost if you lose your wallet.') + ' '\
              + _('To prevent that, you should have a backup of this channel on another device.')
        if self.parent.question(msg):
            def task():
                coro = self.lnworker.force_close_channel(channel_id)
                return self.network.run_from_another_thread(coro)
            WaitingDialog(self, 'please wait..', task, self.on_success, self.on_failure)

    def remove_channel(self, channel_id):
        if self.main_window.question(_('Are you sure you want to delete this channel? This will purge associated transactions from your wallet history.')):
            self.lnworker.remove_channel(channel_id)

    def remove_channel_backup(self, channel_id):
        if self.main_window.question(_('Remove channel backup?')):
            self.lnbackups.remove_channel_backup(channel_id)

    def export_channel_backup(self, channel_id):
        data = self.lnworker.export_channel_backup(channel_id)
        self.main_window.show_qrcode('channel_backup:' + data, 'channel backup')

    def request_force_close(self, channel_id):
        def task():
            coro = self.lnbackups.request_force_close(channel_id)
            return self.network.run_from_another_thread(coro)
        def on_success(b):
            self.main_window.show_message('success')
        WaitingDialog(self, 'please wait..', task, on_success, self.on_failure)

    def create_menu(self, position):
        menu = QMenu()
        menu.setSeparatorsCollapsible(True)  # consecutive separators are merged together
        selected = self.selected_in_column(self.Columns.NODE_ID)
        if not selected:
            return
        multi_select = len(selected) > 1
        if multi_select:
            return
        idx = self.indexAt(position)
        if not idx.isValid():
            return
        item = self.model().itemFromIndex(idx)
        if not item:
            return
        channel_id = idx.sibling(idx.row(), self.Columns.NODE_ID).data(ROLE_CHANNEL_ID)
        if channel_id in self.lnbackups.channel_backups:
            menu.addAction(_("Request force-close"), lambda: self.request_force_close(channel_id))
            menu.addAction(_("Delete"), lambda: self.remove_channel_backup(channel_id))
            menu.exec_(self.viewport().mapToGlobal(position))
            return
        chan = self.lnworker.channels[channel_id]
        menu.addAction(_("Details..."), lambda: self.parent.show_channel(channel_id))
        cc = self.add_copy_menu(menu, idx)
        cc.addAction(_("Long Channel ID"), lambda: self.place_text_on_clipboard(channel_id.hex(),
                                                                                title=_("Long Channel ID")))
        if not chan.is_closed():
            if not chan.is_frozen_for_sending():
                menu.addAction(_("Freeze (for sending)"), lambda: chan.set_frozen_for_sending(True))
            else:
                menu.addAction(_("Unfreeze (for sending)"), lambda: chan.set_frozen_for_sending(False))
            if not chan.is_frozen_for_receiving():
                menu.addAction(_("Freeze (for receiving)"), lambda: chan.set_frozen_for_receiving(True))
            else:
                menu.addAction(_("Unfreeze (for receiving)"), lambda: chan.set_frozen_for_receiving(False))

        funding_tx = self.parent.wallet.db.get_transaction(chan.funding_outpoint.txid)
        if funding_tx:
            menu.addAction(_("View funding transaction"), lambda: self.parent.show_transaction(funding_tx))
        if not chan.is_closed():
            menu.addSeparator()
            if chan.peer_state == peer_states.GOOD:
                menu.addAction(_("Close channel"), lambda: self.close_channel(channel_id))
            menu.addAction(_("Force-close channel"), lambda: self.force_close(channel_id))
        else:
            item = chan.get_closing_height()
            if item:
                txid, height, timestamp = item
                closing_tx = self.lnworker.lnwatcher.db.get_transaction(txid)
                if closing_tx:
                    menu.addAction(_("View closing transaction"), lambda: self.parent.show_transaction(closing_tx))
        menu.addSeparator()
        menu.addAction(_("Export backup"), lambda: self.export_channel_backup(channel_id))
        if chan.is_redeemed():
            menu.addSeparator()
            menu.addAction(_("Delete"), lambda: self.remove_channel(channel_id))
        menu.exec_(self.viewport().mapToGlobal(position))

    @QtCore.pyqtSlot(Channel)
    def do_update_single_row(self, chan: Channel):
        lnworker = self.parent.wallet.lnworker
        if not lnworker:
            return
        for row in range(self.model().rowCount()):
            item = self.model().item(row, self.Columns.NODE_ID)
            if item.data(ROLE_CHANNEL_ID) != chan.channel_id:
                continue
            for column, v in enumerate(self.format_fields(chan)):
                self.model().item(row, column).setData(v, QtCore.Qt.DisplayRole)
            items = [self.model().item(row, column) for column in self.Columns]
            self._update_chan_frozen_bg(chan=chan, items=items)
        self.update_can_send(lnworker)

    @QtCore.pyqtSlot(Abstract_Wallet)
    def do_update_rows(self, wallet):
        if wallet != self.parent.wallet:
            return
        channels = list(wallet.lnworker.channels.values()) if wallet.lnworker else []
        backups = list(wallet.lnbackups.channel_backups.values())
        if wallet.lnworker:
            self.update_can_send(wallet.lnworker)
        self.model().clear()
        self.update_headers(self.headers)
        for chan in channels + backups:
            items = [QtGui.QStandardItem(x) for x in self.format_fields(chan)]
            self.set_editability(items)
            if self._default_item_bg_brush is None:
                self._default_item_bg_brush = items[self.Columns.NODE_ID].background()
            items[self.Columns.NODE_ID].setData(chan.channel_id, ROLE_CHANNEL_ID)
            items[self.Columns.NODE_ID].setFont(QFont(MONOSPACE_FONT))
            items[self.Columns.LOCAL_BALANCE].setFont(QFont(MONOSPACE_FONT))
            items[self.Columns.REMOTE_BALANCE].setFont(QFont(MONOSPACE_FONT))
            self._update_chan_frozen_bg(chan=chan, items=items)
            self.model().insertRow(0, items)

        self.sortByColumn(self.Columns.SHORT_CHANID, Qt.DescendingOrder)

    def _update_chan_frozen_bg(self, *, chan: Channel, items: Sequence[QStandardItem]):
        assert self._default_item_bg_brush is not None
        # frozen for sending
        item = items[self.Columns.LOCAL_BALANCE]
        if chan.is_frozen_for_sending():
            item.setBackground(ColorScheme.BLUE.as_color(True))
            item.setToolTip(_("This channel is frozen for sending. It will not be used for outgoing payments."))
        else:
            item.setBackground(self._default_item_bg_brush)
            item.setToolTip("")
        # frozen for receiving
        item = items[self.Columns.REMOTE_BALANCE]
        if chan.is_frozen_for_receiving():
            item.setBackground(ColorScheme.BLUE.as_color(True))
            item.setToolTip(_("This channel is frozen for receiving. It will not be included in invoices."))
        else:
            item.setBackground(self._default_item_bg_brush)
            item.setToolTip("")

    def update_can_send(self, lnworker: LNWallet):
        msg = _('Can send') + ' ' + self.parent.format_amount(lnworker.num_sats_can_send())\
              + ' ' + self.parent.base_unit() + '; '\
              + _('can receive') + ' ' + self.parent.format_amount(lnworker.num_sats_can_receive())\
              + ' ' + self.parent.base_unit()
        self.can_send_label.setText(msg)

    def get_toolbar(self):
        h = QHBoxLayout()
        self.can_send_label = QLabel('')
        h.addWidget(self.can_send_label)
        h.addStretch()
        h.addWidget(EnterButton(_('Open Channel'), self.new_channel_dialog))
        return h


    def statistics_dialog(self):
        channel_db = self.parent.network.channel_db
        capacity = self.parent.format_amount(channel_db.capacity()) + ' '+ self.parent.base_unit()
        d = WindowModalDialog(self.parent, _('Lightning Network Statistics'))
        d.setMinimumWidth(400)
        vbox = QVBoxLayout(d)
        h = QGridLayout()
        h.addWidget(QLabel(_('Nodes') + ':'), 0, 0)
        h.addWidget(QLabel('{}'.format(channel_db.num_nodes)), 0, 1)
        h.addWidget(QLabel(_('Channels') + ':'), 1, 0)
        h.addWidget(QLabel('{}'.format(channel_db.num_channels)), 1, 1)
        h.addWidget(QLabel(_('Capacity') + ':'), 2, 0)
        h.addWidget(QLabel(capacity), 2, 1)
        vbox.addLayout(h)
        vbox.addLayout(Buttons(OkButton(d)))
        d.exec_()

    def new_channel_dialog(self):
        lnworker = self.parent.wallet.lnworker
        d = WindowModalDialog(self.parent, _('Open Channel'))
        vbox = QVBoxLayout(d)
        vbox.addWidget(QLabel(_('Enter Remote Node ID or connection string or invoice')))
        local_nodeid = FreezableLineEdit()
        local_nodeid.setMinimumWidth(700)
        local_nodeid.setText(bh2u(lnworker.node_keypair.pubkey))
        local_nodeid.setFrozen(True)
        local_nodeid.setCursorPosition(0)
        remote_nodeid = QLineEdit()
        remote_nodeid.setMinimumWidth(700)
        amount_e = BTCAmountEdit(self.parent.get_decimal_point)
        # max button
        def spend_max():
            amount_e.setFrozen(max_button.isChecked())
            if not max_button.isChecked():
                return
            make_tx = self.parent.mktx_for_open_channel('!')
            try:
                tx = make_tx(None)
            except (NotEnoughFunds, NoDynamicFeeEstimates) as e:
                max_button.setChecked(False)
                amount_e.setFrozen(False)
                self.main_window.show_error(str(e))
                return
            amount = tx.output_value()
            amount = min(amount, LN_MAX_FUNDING_SAT)
            amount_e.setAmount(amount)
        max_button = EnterButton(_("Max"), spend_max)
        max_button.setFixedWidth(100)
        max_button.setCheckable(True)
        suggest_button = QPushButton(d, text=_('Suggest'))
        suggest_button.clicked.connect(lambda: remote_nodeid.setText(bh2u(lnworker.suggest_peer() or b'')))
        clear_button = QPushButton(d, text=_('Clear'))
        def on_clear():
            amount_e.setText('')
            remote_nodeid.setText('')
            max_button.setChecked(False)
        clear_button.clicked.connect(on_clear)
        h = QGridLayout()
        h.addWidget(QLabel(_('Your Node ID')), 0, 0)
        h.addWidget(local_nodeid, 0, 1, 1, 3)
        h.addWidget(QLabel(_('Remote Node ID')), 1, 0)
        h.addWidget(remote_nodeid, 1, 1, 1, 3)
        h.addWidget(suggest_button, 2, 1)
        h.addWidget(clear_button, 2, 2)
        h.addWidget(QLabel('Amount'), 3, 0)
        h.addWidget(amount_e, 3, 1)
        h.addWidget(max_button, 3, 2)
        vbox.addLayout(h)
        ok_button = OkButton(d)
        ok_button.setDefault(True)
        vbox.addLayout(Buttons(CancelButton(d), ok_button))
        if not d.exec_():
            return
        if max_button.isChecked() and amount_e.get_amount() < LN_MAX_FUNDING_SAT:
            # if 'max' enabled and amount is strictly less than max allowed,
            # that means we have fewer coins than max allowed, and hence we can
            # spend all coins
            funding_sat = '!'
        else:
            funding_sat = amount_e.get_amount()
        connect_str = str(remote_nodeid.text()).strip()
        if not connect_str or not funding_sat:
            return
        self.parent.open_channel(connect_str, funding_sat, 0)
