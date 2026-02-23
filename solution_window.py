from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout

class SolutionWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Решение огня")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(420, 240)

        lay = QVBoxLayout(self)
        self.label = QLabel("—")
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setStyleSheet("font-size: 14px; font-weight: 700;")
        lay.addWidget(self.label)

        btn_row = QHBoxLayout()
        self.btn_copy = QPushButton("Копировать")
        self.btn_close = QPushButton("Скрыть")
        btn_row.addWidget(self.btn_copy)
        btn_row.addWidget(self.btn_close)
        lay.addLayout(btn_row)

        self.btn_copy.clicked.connect(self.copy_text)
        self.btn_close.clicked.connect(self.hide)

    def set_text(self, text: str):
        self.label.setText(text)

    def copy_text(self):
        cb = self.clipboard()
        if cb:
            cb.setText(self.label.text())

    def clipboard(self):
        try:
            from PySide6.QtGui import QGuiApplication
            return QGuiApplication.clipboard()
        except Exception:
            return None
