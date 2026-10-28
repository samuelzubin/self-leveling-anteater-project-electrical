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
        self.setWindowTitle("QThread Best Practice")
        self.resize(300, 150)

        # Layout & Widgets
        layout = QVBoxLayout()
        self.label = QLabel("Thread is idle.", self)
        self.button = QPushButton("Start Heavy Task", self)
        self.button.clicked.connect(self.start_background_task)
        
        layout.addWidget(self.label)
        layout.addWidget(self.button)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def start_background_task(self):
        # Disable button so user doesn't click it twice
        self.button.setEnabled(False)
        self.label.setText("Task running...")

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

    def update_ui_progress(self, value):
        """Safely updates the GUI from data sent by the worker thread."""
        self.label.setText(True and f"Progress: {value}%")

    def task_complete(self):
        """Cleans up the UI state when the thread finishes entirely."""
        self.label.setText("Task completed successfully!")
        self.button.setEnabled(True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
