from __future__ import division, unicode_literals, print_function, absolute_import

# stdlib imports

import os
import sys
import socket
import Queue  
import logging
import threading
import signal

# Turn on our error catching for all subsequent imports
import labscript_utils.excepthook


# 3rd party imports:

import pandas
import sip

# Have to set PyQt API via sip before importing PyQt:
API_NAMES = ["QDate", "QDateTime", "QString", "QTextStream", "QTime", "QUrl", "QVariant"]
API_VERSION = 2
for name in API_NAMES:
    sip.setapi(name, API_VERSION)

from PyQt4 import QtCore, QtGui
from PyQt4.QtCore import pyqtSignal as Signal
from PyQt4.QtCore import pyqtSlot as Slot

try:
    from labscript_utils import check_version
except ImportError:
    raise ImportError('Require labscript_utils > 2.1.0')
        
check_version('labscript_utils', '2.1', '3')
check_version('qtutils', '1.5.1', '2')
check_version('zprocess', '1.1.2', '2')

import zprocess.locking
from zprocess import ZMQServer
from zmq import ZMQError

from labscript_utils.labconfig import LabConfig, config_prefix
from labscript_utils.setup_logging import setup_logging
import labscript_utils.shared_drive as shared_drive
import lyse

from qtutils import inmain, inmain_later, inmain_decorator, UiLoader, inthread, DisconnectContextManager
from qtutils.outputbox import OutputBox
import qtutils.icons

# Set working directory to lyse folder, resolving symlinks
lyse_dir = os.path.dirname(os.path.realpath(__file__))
os.chdir(lyse_dir)

# Set a meaningful name for zprocess.locking's client id:
zprocess.locking.set_client_process_name('lyse')


def set_win_appusermodel(window_id):
    from labscript_utils.winshell import set_appusermodel, appids, app_descriptions
    icon_path = os.path.abspath('lyse.ico')
    executable = sys.executable.lower()
    if not executable.endswith('w.exe'):
        executable = executable.replace('.exe', 'w.exe')
    relaunch_command = executable + ' ' + os.path.abspath(__file__.replace('.pyc', '.py'))
    relaunch_display_name = app_descriptions['lyse']
    set_appusermodel(window_id, appids['lyse'], icon_path, relaunch_command, relaunch_display_name)
    
    
@inmain_decorator()
def error_dialog(message):
    QtGui.QMessageBox.warning(app.ui, 'lyse', message)

    
@inmain_decorator()
def question_dialog(message):
    reply = QtGui.QMessageBox.question(app.ui, 'lyse', message,
                                       QtGui.QMessageBox.Yes|QtGui.QMessageBox.No)
    return (reply == QtGui.QMessageBox.Yes)
  
  
class WebServer(ZMQServer):

    def handler(self, request_data):
        logger.info('WebServer request: %s'%str(request_data))
        if request_data == 'hello':
            return 'hello'
        elif request_data == 'get dataframe':
            return app.filebox.dataframe
        elif isinstance(request_data, dict):
            if 'filepath' in request_data:
                h5_filepath = labscript_utils.shared_drive.path_to_local(request_data['filepath'])
                app.filebox.incoming_queue.put([h5_filepath])
                return 'added successfully'
        return ("error: operation not supported. Recognised requests are:\n "
                "'get dataframe'\n 'hello'\n {'filepath': <some_h5_filepath>}")
               
               
class LyseMainWindow(QtGui.QMainWindow):
    # A signal for when the window manager has created a new window for this widget:
    newWindow = Signal(int)

    def event(self, event):
        result = QtGui.QMainWindow.event(self, event)
        if event.type() == QtCore.QEvent.WinIdChange:
            self.newWindow.emit(self.effectiveWinId())
        return result

        
class EditColumnsDialog(QtGui.QDialog):
    # A signal for when the window manager has created a new window for this widget:
    newWindow = Signal(int)

    def event(self, event):
        result = QtGui.QDialog.event(self, event)
        if event.type() == QtCore.QEvent.WinIdChange:
            self.newWindow.emit(self.effectiveWinId())
        return result

        
class EditColumns(object):

    def __init__(self, filebox, columns=None):
        loader = UiLoader()
        self.ui = loader.load('edit_columns.ui', EditColumnsDialog())
        self.ui.setWindowModality(QtCore.Qt.ApplicationModal)
        self.connect_signals()
        self.ui.show()
        
    def connect_signals(self):
        if os.name == 'nt':
            self.ui.newWindow.connect(set_win_appusermodel)
            
            
class FileBox(object):
    def __init__(self, container, exp_config, to_singleshot, from_singleshot, to_multishot, from_multishot):
    
        self.exp_config = exp_config
        self.to_singleshot = to_singleshot
        self.to_multishot = to_multishot
        self.from_singleshot = from_singleshot
        self.from_multishot = from_multishot
        
        self.logger = logging.getLogger('LYSE.FileBox')  
        self.logger.info('starting')
        
        loader = UiLoader()
        # loader.registerCustomWidget(TreeView) # unsure if we will be needing this
        self.ui = loader.load('filebox.ui')
        container.addWidget(self.ui)
        
        self.connect_signals()
        
        self.analysis_loop_paused = False
        
        # A condition to let the looping threads know when to recheck conditions
        # they're waiting on (instead of having them do time.sleep)
        self.timing_condition = threading.Condition()
        
        # The folder that the 'add shots' dialog will open to:
        self.current_folder = self.exp_config.get('paths', 'experiment_shot_storage')
        
        # Whether the last scroll to the bottom of the treeview has been processed:
        self.scrolled = True
        
        # A queue for storing incoming files from the ZMQ server so
        # the server can keep receiving files even if analysis is slow
        # or paused:
        self.incoming_queue = Queue.Queue()
        
        # This dataframe will contain all the scalar data
        # from the run files that are currently open:
        index = pandas.MultiIndex.from_tuples([('filepath', '')])
        self.dataframe = pandas.DataFrame({'filepath':[]},columns=index)
        
        # Start the thread to handle incoming files, and store them in
        # a buffer if processing is paused:
        #self.incoming = threading.Thread(target = self.incoming_buffer_loop)
        #self.incoming.daemon = True
        #self.incoming.start()
        
        #self.analysis = threading.Thread(target = self.analysis_loop)
        #self.analysis.daemon = True
        #self.analysis.start()

        #self.adjustment.set_value(self.adjustment.upper - self.adjustment.page_size)
        
    def connect_signals(self):
        self.ui.pushButton_edit_columns.clicked.connect(self.on_edit_columns_clicked)
        
    def on_edit_columns_clicked(self):
        # visible = {}
        # for column in self.treeview.get_columns():
            # label = column.get_widget()
            # if isinstance(label, gtk.Label):
                # title = label.get_text()
                # visible[title] = column.get_visible()
        self.dialog = EditColumns(self)
        

class Lyse(object):
    def __init__(self):
        loader = UiLoader()
        self.ui = loader.load('main.ui', LyseMainWindow())
        
        self.connect_signals()
        
        self.setup_config()
        self.port = int(self.exp_config.get('ports', 'lyse'))
        
        # The singleshot routinebox will be connected to the filebox
        # by queues:
        to_singleshot = Queue.Queue()
        from_singleshot = Queue.Queue()
        
        # So will the multishot routinebox:
        to_multishot = Queue.Queue()
        from_multishot = Queue.Queue()
        
        self.output_box = OutputBox(self.ui.verticalLayout_output_box)
        #self.singleshot_routinebox = RoutineBox(self.ui.verticalLayout_singleshot_routinebox,
        #                                        self, to_singleshot, from_singleshot, self.outputbox.port)
        #self.multishot_routinebox = RoutineBox(self.ui.verticalLayout_multishot_routinebox,
        #                                       self, to_multishot, from_multishot, self.outputbox.port, multishot=True)
        self.filebox = FileBox(self.ui.verticalLayout_filebox,
                               self.exp_config, to_singleshot, from_singleshot, to_multishot, from_multishot)
                               
        self.ui.resize(1600, 900)
        self.ui.show()
        # self.ui.showMaximized()
    
    def setup_config(self):
        config_path = os.path.join(config_prefix, '%s.ini' % socket.gethostname())
        required_config_params = {"DEFAULT":["experiment_name"],
                                  "programs":["text_editor",
                                              "text_editor_arguments",
                                              "hdf5_viewer",
                                              "hdf5_viewer_arguments"],
                                  "paths":["shared_drive",
                                           "experiment_shot_storage",
                                           "analysislib"],
                                  "ports":["lyse"]
                                 }           
        self.exp_config = LabConfig(config_path, required_config_params)
    
    def connect_signals(self):
        if os.name == 'nt':
            self.ui.newWindow.connect(set_win_appusermodel)
    
    def destroy(self,*args):
        raise NotImplementedError
        #gtk.main_quit()
        # The routine boxes have subprocesses that need to be quit:
        #self.singleshot_routinebox.destroy()
        #self.multishot_routinebox.destroy()
        #self.server.shutdown()
        
    ##### TESTING ONLY REMOVE IN PRODUCTION
    def submit_dummy_shots(self):
        path = r'C:\Experiments\rb_chip\connectiontable\2014\10\21\20141021T135341_connectiontable_11.h5'
        print(zprocess.zmq_get(self.port, data={'filepath': path}))



if __name__ == "__main__":
    logger = setup_logging('lyse')
    labscript_utils.excepthook.set_logger(logger)
    logger.info('\n\n===============starting===============\n')
    qapplication = QtGui.QApplication(sys.argv)
    qapplication.setAttribute(QtCore.Qt.AA_DontShowIconsInMenus, False)
    app = Lyse()
    # Start the web server:
    server = WebServer(app.port)
    
    # TEST
    app.submit_dummy_shots()
    
    signal.signal(signal.SIGINT, signal.SIG_DFL) # Quit on ctrl-c
    
    sys.exit(qapplication.exec_())