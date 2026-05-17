import sys
import asyncio
import httpx
import os
import signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLineEdit, QVBoxLayout, QHBoxLayout,
    QWidget, QTextBrowser, QFrame, QPushButton
)
from PySide6.QtCore import Qt, QTimer, Property, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont, QPalette, QColor, QKeyEvent, QIcon
from qasync import QEventLoop, asyncSlot

class SpotlightWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AsistenteIA Spotlight")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Clase para reglas de Hyprland
        self.setObjectName("asistenteia-gui")
        
        # Layout Principal
        self.central_widget = QFrame()
        self.central_widget.setStyleSheet("""
            QFrame {
                background-color: rgba(30, 30, 46, 240);
                border: 2px solid rgba(137, 180, 250, 150);
                border-radius: 15px;
            }
        """)
        self.setCentralWidget(self.central_widget)
        
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(15)

        # Header con Input y Botón de Parar
        self.header_layout = QHBoxLayout()
        
        # Input Field
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Pregunta algo o habla...")
        self.input_field.setFont(QFont("Inter", 16))
        self.input_field.setStyleSheet("""
            QLineEdit {
                background: transparent;
                border: none;
                color: #cdd6f4;
                padding: 5px;
            }
        """)
        self.input_field.returnPressed.connect(self.on_submit)
        self.header_layout.addWidget(self.input_field)

        # Botón para parar TTS / Cancelar
        self.stop_button = QPushButton("⏹")
        self.stop_button.setToolTip("Detener habla / proceso")
        self.stop_button.setFixedSize(35, 35)
        self.stop_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(243, 139, 168, 40);
                border: 1px solid #f38ba8;
                border-radius: 17px;
                color: #f38ba8;
                font-size: 18px;
            }
            QPushButton:hover {
                background-color: rgba(243, 139, 168, 80);
            }
            QPushButton:pressed {
                background-color: #f38ba8;
                color: #1e1e2e;
            }
        """)
        self.stop_button.clicked.connect(self.on_cancel)
        self.header_layout.addWidget(self.stop_button)
        
        self.main_layout.addLayout(self.header_layout)

        # Chat Area (Respuesta)
        self.chat_area = QTextBrowser()
        self.chat_area.setFont(QFont("Inter", 12))
        self.chat_area.setStyleSheet("""
            QTextBrowser {
                background: transparent;
                border: none;
                color: #bac2de;
            }
        """)
        self.chat_area.setOpenExternalLinks(True)
        self.chat_area.hide()
        self.main_layout.addWidget(self.chat_area)

        # Status Polling
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.check_backend_status)
        self.status_timer.start(500) # Comprobar cada 500ms

        # Animación de altura
        self._height = 80
        self.animation = QPropertyAnimation(self, b"windowHeight")
        self.animation.setDuration(300)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)

        # Configuración de tamaño inicial
        self.setFixedSize(600, 80)
        self.position_window()
        
        self.last_recording_state = False
        self.pending_gui_request = False

    @Property(int)
    def windowHeight(self):
        return self._height

    @windowHeight.setter
    def windowHeight(self, height):
        self._height = height
        self.setFixedHeight(height)

    def animate_height(self, target_height):
        if self._height == target_height: return
        self.animation.stop()
        self.animation.setStartValue(self._height)
        self.animation.setEndValue(target_height)
        self.animation.start()

    def position_window(self):
        screen = QApplication.primaryScreen().geometry()
        margin_right = 20
        margin_top = 10 # Justo debajo de la barra de Hyprland
        
        # Situarlo arriba a la derecha
        self.move(
            screen.width() - self.width() - margin_right,
            margin_top
        )

    @asyncSlot()
    async def on_cancel(self):
        """Detiene cualquier proceso o habla en curso."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://127.0.0.1:8765/cancel")
            self.input_field.setPlaceholderText("Interrumpido.")
            QTimer.singleShot(2000, lambda: self.input_field.setPlaceholderText("Pregunta algo o habla..."))
        except Exception as e:
            print(f"Error cancelando: {e}")

    @asyncSlot()
    async def check_backend_status(self):
        """Consulta el estado del backend para reflejarlo en la UI."""
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get("http://127.0.0.1:8765/status")
                if response.status_code == 200:
                    data = response.json()
                    is_recording = data.get("processing", False)
                    
                    if is_recording and not self.last_recording_state:
                        # Empezó a grabar/procesar
                        self.input_field.setPlaceholderText("Escuchando / Procesando...")
                        self.input_field.setEnabled(False)
                        self.central_widget.setStyleSheet("QFrame { background-color: rgba(30, 30, 46, 240); border: 2px solid #f38ba8; border-radius: 15px; }")
                    
                    elif not is_recording and self.last_recording_state:
                        # Terminó de grabar/procesar
                        self.input_field.setPlaceholderText("Pregunta algo o habla...")
                        self.input_field.setEnabled(True)
                        self.central_widget.setStyleSheet("QFrame { background-color: rgba(30, 30, 46, 240); border: 2px solid #89b4fa; border-radius: 15px; }")
                        
                        # Solo actualizar desde el historial si NO fue una petición iniciada por el GUI
                        if not self.isHidden() and not self.pending_gui_request:
                            await self.update_last_response()
                    
                    self.last_recording_state = is_recording
        except:
            pass

    async def update_last_response(self):
        """Actualiza el área de chat con el último par de mensajes (usuario y asistente)."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("http://127.0.0.1:8765/history")
                if response.status_code == 200:
                    data = response.json()
                    history = data.get("history", [])
                    if len(history) >= 2:
                        user_msg = history[-2].get("content", "")
                        assistant_msg = history[-1].get("content", "")
                        
                        full_chat = f"**Tú:** {user_msg}\n\n---\n\n**AsistenteIA:** {assistant_msg}"
                        
                        self.animate_height(450)
                        self.chat_area.show()
                        self.chat_area.setMarkdown(full_chat)
                    elif len(history) == 1:
                        # Caso donde solo hay un mensaje (ej: error o el usuario acaba de hablar)
                        self.chat_area.setMarkdown(f"**Tú:** {history[0].get('content', '')}")
        except Exception as e:
            print(f"Error actualizando historial: {e}")

    @asyncSlot()
    async def on_submit(self):
        text = self.input_field.text().strip()
        if not text or self.pending_gui_request: return

        self.pending_gui_request = True
        self.input_field.setEnabled(False)
        self.input_field.setPlaceholderText("Pensando...")
        self.animate_height(450)
        self.chat_area.show()
        
        # Mostrar lo que el usuario acaba de escribir de inmediato
        self.chat_area.setMarkdown(f"**Tú:** {text}\n\n---\n\n_Buscando respuesta..._")

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post("http://127.0.0.1:8765/transcribe", json={"text": text})
                if response.status_code == 200:
                    data = response.json()
                    response_text = data.get("response_text", "")
                    full_chat = f"**Tú:** {text}\n\n---\n\n**AsistenteIA:** {response_text}"
                    self.chat_area.setMarkdown(full_chat)
                elif response.status_code == 409:
                    pass
                else:
                    self.chat_area.setPlainText(f"Error: {response.status_code}")
        except Exception as e:
            self.chat_area.setPlainText(f"Error de conexión: {e}")
        finally:
            self.pending_gui_request = False
            self.input_field.setEnabled(True)
            self.input_field.clear()
            self.input_field.setFocus()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            if self._height > 80:
                self.animate_height(80)
                QTimer.singleShot(300, self.chat_area.hide)
            else:
                self.hide()
        super().keyPressEvent(event)

async def main():
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = SpotlightWindow()
    window.show()

    # Manejadores de señales para control externo
    def handle_hide():
        window.hide()
    
    def handle_exit():
        app.quit()

    def handle_show():
        window.show()
        window.raise_()
        window.activateWindow()

    # Usar el loop de asyncio para manejar señales de forma segura con Qt
    for sig in (signal.SIGUSR1,):
        loop.add_signal_handler(sig, handle_hide)
    for sig in (signal.SIGUSR2,):
        loop.add_signal_handler(sig, handle_show)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_exit)

    with loop:
        await loop.run_forever()

if __name__ == "__main__":
    pid_file = "/tmp/asistenteia-gui.pid"
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0) 
            sys.exit(0)
        except (OSError, ValueError):
            pass
    
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))
        
    try:
        asyncio.run(main())
    finally:
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    current_pid = int(f.read().strip())
                if current_pid == os.getpid():
                    os.remove(pid_file)
            except:
                pass
