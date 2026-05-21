import sys
import asyncio
import httpx
import os
import signal
import math
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLineEdit, QVBoxLayout, QHBoxLayout,
    QWidget, QTextBrowser, QFrame, QPushButton
)
from PySide6.QtCore import Qt, QTimer, Property, QPropertyAnimation, QEasingCurve, QPointF
from PySide6.QtGui import QFont, QPalette, QColor, QKeyEvent, QIcon, QPainter, QPen, QBrush, QLinearGradient, QPainterPath
from qasync import QEventLoop, asyncSlot

class StateVisualizer(QWidget):
    """Visualizador dinámico que renderiza animaciones basadas en el estado del asistente."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(60, 30)
        self.state = "inactive"  # inactive, listening, thinking
        self.phase = 0.0
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_animation)
        self.timer.start(30)  # ~33 fps
        
    def set_state(self, state):
        if self.state != state:
            self.state = state
            self.update()
            
    def update_animation(self):
        if self.state != "inactive":
            self.phase += 0.15
            self.update()
            
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        w = self.width()
        h = self.height()
        
        if self.state == "listening":
            # 3 círculos pulsantes con colores Catppuccin
            painter.setPen(Qt.NoPen)
            colors = [
                QColor(243, 139, 168, 200),  # Red
                QColor(137, 180, 250, 200),  # Blue
                QColor(166, 227, 161, 200)   # Green
            ]
            for i, color in enumerate(colors):
                offset = i * (math.pi / 3)
                scale = 0.5 + 0.4 * math.sin(self.phase + offset)
                radius = 8 * scale
                x = w / 2 + (i - 1) * 16
                y = h / 2
                painter.setBrush(QBrush(color))
                painter.drawEllipse(QPointF(x, y), radius, radius)
                
        elif self.state == "thinking":
            # Onda senoidal con gradiente de neon en movimiento continuo
            painter.setPen(Qt.NoPen)
            gradient = QLinearGradient(0, 0, w, 0)
            gradient.setColorAt(0.0, QColor(137, 180, 250))  # Lavender/Blue
            gradient.setColorAt(0.5, QColor(203, 166, 247))  # Mauve
            gradient.setColorAt(1.0, QColor(137, 180, 250))
            
            # Desplazar gradiente con el tiempo
            shift = (self.phase * 5) % w
            gradient.setStart(shift, 0)
            gradient.setFinalStop(shift + w, 0)
            
            painter.setPen(QPen(QBrush(gradient), 3, Qt.SolidLine, Qt.RoundCap))
            path = QPainterPath()
            path.moveTo(5, h / 2)
            for x in range(5, w - 5):
                y = h / 2 + 7 * math.sin(self.phase + x * 0.15)
                path.lineTo(x, y)
            painter.drawPath(path)
            
        else:
            # Latido verde minimalista (estado inactivo / listo)
            painter.setPen(Qt.NoPen)
            # Pulso lento de fondo
            pulse = 0.2 + 0.15 * math.sin(self.phase * 0.2)
            painter.setBrush(QBrush(QColor(166, 227, 161, int(255 * pulse))))
            painter.drawEllipse(QPointF(w / 2, h / 2), 6, 6)
            
            # Centro sólido
            painter.setBrush(QBrush(QColor(166, 227, 161, 255)))
            painter.drawEllipse(QPointF(w / 2, h / 2), 3, 3)

class SpotlightWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AsistenteIA Spotlight")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Clase para reglas de Hyprland
        self.setObjectName("asistenteia-gui")
        
        # Historial de entradas local (estilo terminal)
        self.input_history = []
        self.history_index = -1
        self.temp_input = ""
        
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
        self.main_layout.setContentsMargins(18, 15, 18, 15)
        self.main_layout.setSpacing(12)

        # Header con Input, Visualizador y Botón de Parar
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

        # Visualizador de Estado
        self.visualizer = StateVisualizer()
        self.header_layout.addWidget(self.visualizer)

        # Botón para parar TTS / Cancelar
        self.stop_button = QPushButton("⏹")
        self.stop_button.setToolTip("Detener habla / proceso")
        self.stop_button.setFixedSize(32, 32)
        self.stop_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(243, 139, 168, 30);
                border: 1px solid rgba(243, 139, 168, 120);
                border-radius: 16px;
                color: #f38ba8;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: rgba(243, 139, 168, 60);
            }
            QPushButton:pressed {
                background-color: #f38ba8;
                color: #1e1e2e;
            }
        """)
        self.stop_button.clicked.connect(self.on_cancel)
        self.header_layout.addWidget(self.stop_button)

        # Botón para minimizar / ocultar panel
        self.minimize_button = QPushButton("🗕")
        self.minimize_button.setToolTip("Minimizar / Ocultar panel")
        self.minimize_button.setFixedSize(32, 32)
        self.minimize_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(137, 180, 250, 30);
                border: 1px solid rgba(137, 180, 250, 120);
                border-radius: 16px;
                color: #89b4fa;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: rgba(137, 180, 250, 60);
            }
            QPushButton:pressed {
                background-color: #89b4fa;
                color: #1e1e2e;
            }
        """)
        self.minimize_button.clicked.connect(self.on_minimize)
        self.header_layout.addWidget(self.minimize_button)
        
        self.main_layout.addLayout(self.header_layout)

        # Chat Area (Respuesta en Markdown con estilos CSS hermosos)
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
        
        # Configurar hoja de estilos del documento para renderizado rico
        self.chat_area.document().setDefaultStyleSheet("""
            body {
                font-family: 'Inter', sans-serif;
                color: #cdd6f4;
                font-size: 13px;
                line-height: 1.5;
            }
            p {
                margin: 4px 0 8px 0;
            }
            strong {
                color: #89b4fa;
                font-size: 13px;
            }
            hr {
                border: none;
                border-top: 1px solid rgba(137, 180, 250, 60);
                margin: 12px 0;
            }
            pre {
                background-color: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 10px;
                color: #a6e3a1;
                font-family: 'JetBrains Mono', 'Courier New', monospace;
                font-size: 12px;
                margin: 8px 0;
            }
            code {
                background-color: #11111b;
                color: #f5c2e7;
                font-family: 'JetBrains Mono', 'Courier New', monospace;
                font-size: 12px;
                padding: 1px 4px;
                border-radius: 4px;
            }
            blockquote {
                border-left: 3px solid #f38ba8;
                background-color: rgba(243, 139, 168, 20);
                color: #bac2de;
                font-style: italic;
                padding: 6px 10px;
                margin: 8px 0;
                border-radius: 4px;
            }
            ul, ol {
                margin: 4px 0 8px 0;
                padding-left: 20px;
            }
            li {
                margin-bottom: 3px;
            }
        """)

        # Barra de Estado (Status Pills)
        self.bottom_bar = QFrame()
        self.bottom_bar.setStyleSheet("QFrame { background: transparent; border: none; }")
        self.bottom_layout = QHBoxLayout(self.bottom_bar)
        self.bottom_layout.setContentsMargins(5, 5, 5, 0)
        self.bottom_layout.setSpacing(8)
        
        # Pill para Modelo (LiteRT)
        self.model_pill = QPushButton("🧠 LiteRT")
        self.model_pill.setEnabled(False)
        self.model_pill.setStyleSheet("""
            QPushButton {
                background-color: rgba(166, 227, 161, 20);
                border: 1px solid rgba(166, 227, 161, 80);
                border-radius: 10px;
                color: #a6e3a1;
                font-family: 'Inter';
                font-size: 10px;
                padding: 2px 8px;
            }
        """)
        self.bottom_layout.addWidget(self.model_pill)
        
        # Pill para Audio
        self.audio_pill = QPushButton("󰍬 Audio: --")
        self.audio_pill.setEnabled(False)
        self.audio_pill.setStyleSheet("""
            QPushButton {
                background-color: rgba(137, 180, 250, 20);
                border: 1px solid rgba(137, 180, 250, 80);
                border-radius: 10px;
                color: #89b4fa;
                font-family: 'Inter';
                font-size: 10px;
                padding: 2px 8px;
            }
        """)
        self.bottom_layout.addWidget(self.audio_pill)
        
        # Pill para Historial
        self.history_pill = QPushButton("󰅪 0 msgs")
        self.history_pill.setEnabled(False)
        self.history_pill.setStyleSheet("""
            QPushButton {
                background-color: rgba(245, 194, 231, 20);
                border: 1px solid rgba(245, 194, 231, 80);
                border-radius: 10px;
                color: #f5c2e7;
                font-family: 'Inter';
                font-size: 10px;
                padding: 2px 8px;
            }
        """)
        self.bottom_layout.addWidget(self.history_pill)
        
        self.bottom_layout.addStretch()
        
        # Atajos Rápidos
        self.shortcuts_label = QPushButton("ESC: Ocultar | Ctrl+R: Reset | Ctrl+H: Historial")
        self.shortcuts_label.setEnabled(False)
        self.shortcuts_label.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                color: #6c7086;
                font-family: 'Inter';
                font-size: 10px;
            }
        """)
        self.bottom_layout.addWidget(self.shortcuts_label)
        
        self.main_layout.addWidget(self.bottom_bar)

        # Status Polling
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.check_backend_status)
        self.status_timer.start(500)  # Comprobar cada 500ms

        # Animación de altura (Altura inicial 110px para acomodar barra inferior)
        self._height = 110
        self.animation = QPropertyAnimation(self, b"windowHeight")
        self.animation.setDuration(300)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)

        # Configuración de tamaño inicial
        self.resize(600, 110)
        self.setFixedHeight(110)
        
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

    @asyncSlot()
    async def on_cancel(self):
        """Detiene cualquier proceso o habla en curso."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://127.0.0.1:8765/cancel")
            self.input_field.setPlaceholderText("Interrumpido.")
            self.visualizer.set_state("inactive")
            QTimer.singleShot(2000, lambda: self.input_field.setPlaceholderText("Pregunta algo o habla..."))
        except Exception as e:
            print(f"Error cancelando: {e}")

    def on_minimize(self):
        """Si la ventana está expandida, la colapsa. Si ya está colapsada, la oculta."""
        if self._height > 110:
            self.animate_height(110)
            QTimer.singleShot(300, self.chat_area.hide)
        else:
            self.hide()

    @asyncSlot()
    async def on_reset(self):
        """Reinicia el historial de conversación en el backend."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://127.0.0.1:8765/reset")
            self.chat_area.clear()
            self.chat_area.hide()
            self.animate_height(110)
            self.input_field.setPlaceholderText("Historial reiniciado.")
            self.visualizer.set_state("inactive")
            QTimer.singleShot(2000, lambda: self.input_field.setPlaceholderText("Pregunta algo o habla..."))
        except Exception as e:
            print(f"Error reiniciando: {e}")

    @asyncSlot()
    async def check_backend_status(self):
        """Consulta el estado del backend para reflejarlo en la UI."""
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get("http://127.0.0.1:8765/status")
                if response.status_code == 200:
                    data = response.json()
                    
                    litert_connected = data.get("litert_connected", False)
                    bluetooth_audio = data.get("bluetooth_audio", "Desconectado")
                    conversation_length = data.get("conversation_length", 0)
                    is_processing = data.get("processing", False)
                    
                    # Actualizar visualizador animado
                    if is_processing:
                        # Si está escuchando en el micrófono
                        if "Escuchando" in self.input_field.placeholderText():
                            self.visualizer.set_state("listening")
                        else:
                            self.visualizer.set_state("thinking")
                    else:
                        self.visualizer.set_state("inactive")
                    
                    # Actualizar píldoras (Status Pills)
                    if litert_connected:
                        self.model_pill.setText("🧠 LiteRT: Listo")
                        self.model_pill.setStyleSheet("""
                            QPushButton {
                                background-color: rgba(166, 227, 161, 20);
                                border: 1px solid rgba(166, 227, 161, 100);
                                border-radius: 10px;
                                color: #a6e3a1;
                                font-family: 'Inter';
                                font-size: 10px;
                                padding: 2px 8px;
                            }
                        """)
                    else:
                        self.model_pill.setText("🧠 LiteRT: Error")
                        self.model_pill.setStyleSheet("""
                            QPushButton {
                                background-color: rgba(243, 139, 168, 20);
                                border: 1px solid rgba(243, 139, 168, 100);
                                border-radius: 10px;
                                color: #f38ba8;
                                font-family: 'Inter';
                                font-size: 10px;
                                padding: 2px 8px;
                            }
                        """)
                    
                    # Audio Pill acortado
                    audio_text = bluetooth_audio.split(" (")[0]
                    self.audio_pill.setText(f"󰓃 {audio_text}")
                    
                    # History Pill
                    self.history_pill.setText(f"󰅪 {conversation_length} msgs")
                    
                    # Cambios de estilo y placeholder en input principal
                    if is_processing and not self.last_recording_state:
                        if "Escuchando" in self.input_field.placeholderText():
                            self.input_field.setPlaceholderText("Escuchando...")
                        else:
                            self.input_field.setPlaceholderText("Pensando...")
                        self.input_field.setEnabled(False)
                        self.central_widget.setStyleSheet("""
                            QFrame {
                                background-color: rgba(30, 30, 46, 240);
                                border: 2px solid #f38ba8;
                                border-radius: 15px;
                            }
                        """)
                    
                    elif not is_processing and self.last_recording_state:
                        self.input_field.setPlaceholderText("Pregunta algo o habla...")
                        self.input_field.setEnabled(True)
                        self.central_widget.setStyleSheet("""
                            QFrame {
                                background-color: rgba(30, 30, 46, 240);
                                border: 2px solid #89b4fa;
                                border-radius: 15px;
                            }
                        """)
                        
                        # Actualizar automáticamente tras finalizar
                        if not self.isHidden() and not self.pending_gui_request:
                            await self.update_last_response()
                    
                    self.last_recording_state = is_processing
        except Exception as e:
            # Backend fuera de servicio
            self.model_pill.setText("🧠 Desconectado")
            self.model_pill.setStyleSheet("""
                QPushButton {
                    background-color: rgba(108, 112, 134, 20);
                    border: 1px solid rgba(108, 112, 134, 80);
                    border-radius: 10px;
                    color: #6c7086;
                    font-family: 'Inter';
                    font-size: 10px;
                    padding: 2px 8px;
                }
            """)
            self.visualizer.set_state("inactive")

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
                        
                        self.animate_height(480)
                        self.chat_area.show()
                        self.chat_area.setMarkdown(full_chat)
                    elif len(history) == 1:
                        self.animate_height(480)
                        self.chat_area.show()
                        self.chat_area.setMarkdown(f"**Tú:** {history[0].get('content', '')}")
        except Exception as e:
            print(f"Error actualizando historial: {e}")

    @asyncSlot()
    async def on_submit(self):
        text = self.input_field.text().strip()
        if not text or self.pending_gui_request: return

        # Guardar en el historial local de comandos
        if not self.input_history or self.input_history[-1] != text:
            self.input_history.append(text)
        self.history_index = -1
        self.temp_input = ""

        self.pending_gui_request = True
        self.input_field.setEnabled(False)
        self.input_field.setPlaceholderText("Pensando...")
        self.animate_height(480)
        self.chat_area.show()
        
        self.visualizer.set_state("thinking")
        
        # Mostrar lo que el usuario escribe inmediatamente
        self.chat_area.setMarkdown(f"**Tú:** {text}\n\n---\n\n**AsistenteIA:** ...")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", "http://127.0.0.1:8765/transcribe/stream", json={"text": text}) as response:
                    if response.status_code == 200:
                        first_chunk = True
                        accumulated_response = ""
                        async for chunk in response.aiter_text():
                            if first_chunk:
                                first_chunk = False
                                self.chat_area.setMarkdown(f"**Tú:** {text}\n\n---\n\n**AsistenteIA:** ")
                            accumulated_response += chunk
                            full_chat = f"**Tú:** {text}\n\n---\n\n**AsistenteIA:** {accumulated_response}"
                            self.chat_area.setMarkdown(full_chat)
                            
                            # Auto-scroll al final
                            self.chat_area.verticalScrollBar().setValue(
                                self.chat_area.verticalScrollBar().maximum()
                            )
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
        # ESC para minimizar o cerrar panel
        if event.key() == Qt.Key_Escape:
            if self._height > 110:
                self.animate_height(110)
                QTimer.singleShot(300, self.chat_area.hide)
            else:
                self.hide()
                
        # Ctrl+R para Reiniciar
        elif event.key() == Qt.Key_R and event.modifiers() & Qt.ControlModifier:
            asyncio.create_task(self.on_reset())
            
        # Ctrl+H para consultar Historial manual
        elif event.key() == Qt.Key_H and event.modifiers() & Qt.ControlModifier:
            asyncio.create_task(self.update_last_response())
            
        # Navegación del historial de comandos con flechas de teclado
        elif event.key() == Qt.Key_Up:
            if self.input_field.hasFocus():
                if self.history_index == -1:
                    self.temp_input = self.input_field.text()
                
                if len(self.input_history) > 0:
                    if self.history_index == -1:
                        self.history_index = len(self.input_history) - 1
                    elif self.history_index > 0:
                        self.history_index -= 1
                    
                    self.input_field.setText(self.input_history[self.history_index])
                event.accept()
                return
                
        elif event.key() == Qt.Key_Down:
            if self.input_field.hasFocus():
                if self.history_index != -1:
                    if self.history_index < len(self.input_history) - 1:
                        self.history_index += 1
                        self.input_field.setText(self.input_history[self.history_index])
                    else:
                        self.history_index = -1
                        self.input_field.setText(self.temp_input)
                event.accept()
                return
                
        super().keyPressEvent(event)

def main():
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
        loop.run_forever()

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
        main()
    finally:
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    current_pid = int(f.read().strip())
                if current_pid == os.getpid():
                    os.remove(pid_file)
            except:
                pass
        pass
