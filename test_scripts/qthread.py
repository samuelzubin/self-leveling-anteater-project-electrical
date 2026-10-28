import sys
import time
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget, QLabel
from PyQt6.QtCore import QObject, QThread, pyqtSignal

class Worker(QObject):
    # Define signals to communicate with the main thread
    progress = pyqtSignal(int)
    finished = pyqtSignal()

    def run(self):
        for i in range(1, 6):
            time.sleep(1)
            self.progress.emit(i * 20)
        self.finished.emit()


# 2. Setup your main Window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PID Calibration Tool")

        # Layout & Widgets
        layout = QVBoxLayout()

        self.kpLabel = QLabel("Kp: ")
        self.kdLabel = QLabel("Kd: ")
        self.kiLabel = QLabel("Ki: ")
        self.kpButton = QPushButton("Begin kp Calibration...", self)
        self.kpButton.clicked.connect(self.startKpCalibration)
        self.kdButton = QPushButton("Begin ki Calibration...", self)
        self.kdButton.clicked.connect(self.startKdCalibration)
        self.kiButton = QPushButton("Begin kd Calibration...", self)
        self.kiButton.clicked.connect(self.startKiCalibration)

        layout.addWidget(self.kpLabel)
        layout.addWidget(self.kdLabel)
        layout.addWidget(self.kiLabel)
        layout.addWidget(self.kpButton)
        layout.addWidget(self.kdButton)
        layout.addWidget(self.kiButton)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def startKpCalibration(self):
        # Disable button so user doesn't click it twice
        self.kpButton.setEnabled(False)
        self.kdButton.setEnabled(False)
        self.kiButton.setEnabled(False)

        # Step 1: Create the QThread and Worker instances
        self.thread = QThread()
        self.worker = Worker()

        # Step 2: Move the worker to the new thread
        self.worker.moveToThread(self.thread)

        # Step 3: Connect signals and slots
        # When the thread starts, execute the worker's run method
        self.thread.started.connect(self.worker.run)
        
        # Connect worker signals to local UI methods
        self.worker.progress.connect(self.update_ui_progress)
        self.worker.finished.connect(self.task_complete)

        # Step 4: Ensure proper cleanup to avoid memory leaks
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        # Step 5: Start the thread
        self.thread.start()

    def startKdCalibration(self):
        pass   
    def startKiCalibration(self):
        pass

    def update_ui_progress(self, value):
        """Safely updates the GUI from data sent by the worker thread."""
        self.kpLabel.setText(True and f"Kp: {value}%")

    def task_complete(self):
        """Cleans up the UI state when the thread finishes entirely."""
        self.kpLabel.setText("Kp saved!")
        self.kpButton.setEnabled(True)
        self.kdButton.setEnabled(True)
        self.kdButton.setEnabled(True)
    

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
