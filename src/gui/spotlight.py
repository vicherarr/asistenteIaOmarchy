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
from PySide6.QtGui import QFont, QPalette, QColor, QKeyEvent, QIcon, QPainter, QPen, QBrush, QLinearGradient, QRadialGradient, QPainterPath
from qasync import QEventLoop, asyncSlot

class StateVisualizer(QWidget):
    """Visualizador dinámico que renderiza animaciones basadas en el estado del asistente."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(60, 30)
        self.state = "inactive"  # inactive, listening, thinking, speaking
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
            
        elif self.state == "speaking":
            # KITT Scanner - luz roja rebotando con estela
            painter.setPen(Qt.NoPen)
            
            # Posición del scanner (rebote sinusoidal)
            scanner_pos = 0.5 + 0.4 * math.sin(self.phase * 0.8)
            scanner_x = w * scanner_pos
            
            # Estela (trail) con puntos más pequeños y transparentes
            trail_points = 12
            for i in range(trail_points, 0, -1):
                trail_phase = self.phase * 0.8 - i * 0.15
                trail_pos = 0.5 + 0.4 * math.sin(trail_phase)
                trail_x = w * trail_pos
                alpha = int(180 * (1 - i / trail_points))
                radius = 4 * (1 - i / trail_points) + 1
                
                # Brillo rojo estilo KITT
                glow = QRadialGradient(QPointF(trail_x, h / 2), radius * 2)
                glow.setColorAt(0, QColor(235, 50, 50, alpha))
                glow.setColorAt(0.5, QColor(200, 30, 30, int(alpha * 0.5)))
                glow.setColorAt(1, QColor(150, 20, 20, 0))
                
                painter.setBrush(QBrush(glow))
                painter.drawEllipse(QPointF(trail_x, h / 2), radius * 2, radius * 2)
            
            # Cabeza principal del scanner (brillo intenso)
            head_glow = QRadialGradient(QPointF(scanner_x, h / 2), 12)
            head_glow.setColorAt(0, QColor(255, 80, 80, 255))
            head_glow.setColorAt(0.3, QColor(235, 40, 40, 200))
            head_glow.setColorAt(0.7, QColor(180, 20, 20, 80))
            head_glow.setColorAt(1, QColor(120, 10, 10, 0))
            
            painter.setBrush(QBrush(head_glow))
            painter.drawEllipse(QPointF(scanner_x, h / 2), 12, 12)
            
            # Centro blanco brillante
            painter.setBrush(QBrush(QColor(255, 220, 220, 255)))
            painter.drawEllipse(QPointF(scanner_x, h / 2), 3, 3)
            
            # Línea horizontal sutil (barra del scanner)
            painter.setPen(QPen(QColor(60, 20, 20, 80), 1))
            painter.drawLine(5, h / 2, w - 5, h / 2)
            
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

class PremiumStopButton(QPushButton):
    """
    Un botón de detener/habla premium, interactivo y con animaciones de tipo radar/onda,
    gradientes dinámicos de Catppuccin y efectos de brillo (glow) de última generación.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(36, 36)
        self.setCursor(Qt.PointingHandCursor)
        
        # Estados: "inactive", "active", "offline"
        self._state = "inactive"
        
        # Propiedades de animación
        self._hover_progress = 0.0      # De 0.0 a 1.0
        self._press_progress = 0.0      # De 0.0 a 1.0
        self._pulse_phase = 0.0         # Ángulo del pulso sinusoidal
        self._ripple_radius = 0.0       # Radio del ripple de interrupción
        self._ripple_opacity = 0.0      # Opacidad del ripple
        
        # Animadores
        self.hover_animator = QPropertyAnimation(self, b"hoverProgress")
        self.hover_animator.setDuration(200)
        self.hover_animator.setEasingCurve(QEasingCurve.OutCubic)
        
        self.press_animator = QPropertyAnimation(self, b"pressProgress")
        self.press_animator.setDuration(120)
        self.press_animator.setEasingCurve(QEasingCurve.OutCubic)
        
        # Timer para animación de pulso y ripples activos
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_time)
        self.timer.start(16)  # ~60 FPS
        
        self.setMouseTracking(True)
        
    @Property(float)
    def hoverProgress(self):
        return self._hover_progress
        
    @hoverProgress.setter
    def hoverProgress(self, val):
        self._hover_progress = val
        self.update()
        
    @Property(float)
    def pressProgress(self):
        return self._press_progress
        
    @pressProgress.setter
    def pressProgress(self, val):
        self._press_progress = val
        self.update()
        
    def set_state(self, state):
        if self._state != state:
            self._state = state
            if state == "active":
                self.setToolTip("Interrumpir proceso activo")
            elif state == "inactive":
                self.setToolTip("Detener habla / proceso")
            else:
                self.setToolTip("Backend desconectado")
            self.update()
            
    def _update_time(self):
        # Avanzar fase de pulso si está activo
        if self._state == "active":
            self._pulse_phase += 0.08
            self.update()
            
        # Animar ripple
        if self._ripple_opacity > 0.01:
            self._ripple_radius += 1.5
            self._ripple_opacity -= 0.08
            self.update()
            
    def enterEvent(self, event):
        self.hover_animator.stop()
        self.hover_animator.setStartValue(self._hover_progress)
        self.hover_animator.setEndValue(1.0)
        self.hover_animator.start()
        super().enterEvent(event)
        
    def leaveEvent(self, event):
        self.hover_animator.stop()
        self.hover_animator.setStartValue(self._hover_progress)
        self.hover_animator.setEndValue(0.0)
        self.hover_animator.start()
        super().leaveEvent(event)
        
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.press_animator.stop()
            self.press_animator.setStartValue(self._press_progress)
            self.press_animator.setEndValue(1.0)
            self.press_animator.start()
            
            # Ripple de clic
            self._ripple_radius = 5.0
            self._ripple_opacity = 0.8
        super().mousePressEvent(event)
        
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.press_animator.stop()
            self.press_animator.setStartValue(self._press_progress)
            self.press_animator.setEndValue(0.0)
            self.press_animator.start()
        super().mouseReleaseEvent(event)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        w = self.width()
        h = self.height()
        cx = w / 2.0
        cy = h / 2.0
        
        # 1. Dibujar onda expansiva o radar pulsante en estado activo
        if self._state == "active":
            for i in range(2):
                phase_offset = i * math.pi
                scale = 1.0 + 0.35 * math.sin(self._pulse_phase + phase_offset)
                opacity = int(60 * (1.0 - (scale - 0.65) / 0.7))
                if opacity < 0: opacity = 0
                if opacity > 255: opacity = 255
                
                pulse_r = (w / 2.0 - 4) * scale
                painter.setPen(QPen(QColor(243, 139, 168, opacity), 1.5))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), pulse_r, pulse_r)
                
        # 2. Dibujar Ripple de clic
        if self._ripple_opacity > 0.01:
            opacity_int = int(255 * self._ripple_opacity)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(243, 139, 168, opacity_int)))
            painter.drawEllipse(QPointF(cx, cy), self._ripple_radius, self._ripple_radius)
            
        # 3. Dibujar fondo del botón con vidrio y gradiente
        base_radius = (w / 2.0) - 4
        scale_factor = 1.0 - (0.08 * self._press_progress)
        draw_r = base_radius * scale_factor
        
        if self._state == "active":
            # Degradado premium rojo coral / melocotón brillante
            gradient = QLinearGradient(0, 0, 0, h)
            c1 = QColor(243, 139, 168)  # Red
            c2 = QColor(250, 179, 135)  # Peach
            
            if self._hover_progress > 0.01:
                # Mezclar más hacia rosa/púrpura en hover
                c1 = QColor(245, 194, 231)  # Pink
                c2 = QColor(203, 166, 247)  # Mauve
                
            gradient.setColorAt(0.0, c1)
            gradient.setColorAt(1.0, c2)
            
            # Efecto glow de fondo
            glow_intensity = int(80 + 40 * math.sin(self._pulse_phase))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(243, 139, 168, glow_intensity)))
            painter.drawEllipse(QPointF(cx, cy), draw_r + 2, draw_r + 2)
            
            # Fondo principal
            painter.setBrush(QBrush(gradient))
            painter.drawEllipse(QPointF(cx, cy), draw_r, draw_r)
            
        elif self._state == "inactive":
            # Glassmorphism gris/azulino Catppuccin
            bg_opacity = int(25 + 40 * self._hover_progress)
            border_opacity = int(50 + 100 * self._hover_progress)
            
            # Fondo
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(108, 112, 134, bg_opacity)))
            painter.drawEllipse(QPointF(cx, cy), draw_r, draw_r)
            
            # Borde
            border_color = QColor(137, 180, 250, border_opacity) if self._hover_progress > 0.01 else QColor(108, 112, 134, border_opacity)
            painter.setPen(QPen(border_color, 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), draw_r, draw_r)
            
        else:  # offline
            # Muted
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(88, 91, 112, 15)))
            painter.drawEllipse(QPointF(cx, cy), draw_r, draw_r)
            
            painter.setPen(QPen(QColor(88, 91, 112, 40), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), draw_r, draw_r)
            
        # 4. Dibujar el ícono central (Vectorizado)
        icon_size = 8.0 if self._state == "inactive" else 10.0
        icon_size = icon_size * (1.0 + 0.15 * self._hover_progress) * scale_factor
        
        if self._state == "active":
            # Icono de STOP en color oscuro contrastante
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(30, 30, 46)))
            
            rect_path = QPainterPath()
            rect_path.addRoundedRect(
                cx - icon_size / 2.0, 
                cy - icon_size / 2.0, 
                icon_size, 
                icon_size, 
                2.0, 
                2.0
            )
            painter.drawPath(rect_path)
            
        elif self._state == "inactive":
            # Standby: sutil contorno
            icon_color = QColor(243, 139, 168) if self._hover_progress > 0.01 else QColor(108, 112, 134)
            painter.setPen(QPen(icon_color, 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            
            rect_path = QPainterPath()
            rect_path.addRoundedRect(
                cx - icon_size / 2.0, 
                cy - icon_size / 2.0, 
                icon_size, 
                icon_size, 
                1.5, 
                1.5
            )
            painter.drawPath(rect_path)
            
        else:  # offline
            painter.setPen(QPen(QColor(88, 91, 112, 100), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            rect_path = QPainterPath()
            rect_path.addRoundedRect(
                cx - icon_size / 2.0, 
                cy - icon_size / 2.0, 
                icon_size, 
                icon_size, 
                1.5, 
                1.5
            )
            painter.drawPath(rect_path)

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

        # Botón para parar TTS / Cancelar (PremiumStopButton animador)
        self.stop_button = PremiumStopButton()
        self.stop_button.clicked.connect(self.on_cancel)
        self.header_layout.addWidget(self.stop_button)
        
        self.main_layout.addLayout(self.header_layout)

        # Chat Area (Respuesta en Markdown con estilos CSS hermosos)
        self.chat_area = QTextBrowser()
        self.chat_area.setFont(QFont("Inter", 12))
        self.chat_area.setMinimumHeight(0)
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
        
        # Pill para Acelerador (GPU/CPU verídico)
        self.accel_pill = QPushButton("⚙️ CPU")
        self.accel_pill.setEnabled(False)
        self.accel_pill.setStyleSheet("""
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
        self.bottom_layout.addWidget(self.accel_pill)

        
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
        self._resetting = False

    @Property(int)
    def windowHeight(self):
        return self._height

    @windowHeight.setter
    def windowHeight(self, height):
        self._height = height
        if hasattr(self, 'chat_area') and self.chat_area.isVisible():
            self.chat_area.setMaximumHeight(max(0, height - 110))
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
            # Incrementar época para desvincular cualquier tarea asíncrona de UI activa
            self.current_request_id = getattr(self, 'current_request_id', 0) + 1
            self.pending_gui_request = False
            
            # Feedback visual inmediato
            self.stop_button.set_state("inactive")
            
            async with httpx.AsyncClient() as client:
                await client.post("http://127.0.0.1:8765/cancel")
            self.input_field.setPlaceholderText("Interrumpido.")
            self.visualizer.set_state("inactive")
            QTimer.singleShot(2000, lambda: self.input_field.setPlaceholderText("Pregunta algo o habla..."))
        except Exception as e:
            print(f"Error cancelando: {e}")

    @asyncSlot()
    async def on_reset(self):
        """Reinicia el historial de conversacion en el backend."""
        if self._resetting:
            return
        self._resetting = True
        try:
            # Incrementar época de petición para invalidar cualquier stream asíncrono activo en la UI
            self.current_request_id = getattr(self, 'current_request_id', 0) + 1
            self.pending_gui_request = False
            
            # Reset visual inmediato
            self.stop_button.set_state("inactive")
            self.visualizer.set_state("inactive")
            self.chat_area.clear()
            self.chat_area.hide()
                
            self.input_field.setEnabled(True)
            self.input_field.clear()
            self.input_field.setPlaceholderText("Historial reiniciado.")
            QTimer.singleShot(2000, lambda: self.input_field.setPlaceholderText("Pregunta algo o habla..."))
            QTimer.singleShot(500, lambda: setattr(self, '_resetting', False))
            
            # Llamada al backend en segundo plano desacoplada de la UI
            async with httpx.AsyncClient() as client:
                await client.post("http://127.0.0.1:8765/reset")
        except Exception as e:
            print(f"Error reiniciando: {e}")
            self._resetting = False

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
                    
                    # Actualizar visualizador animado y botón Stop dinámicamente
                    is_speaking = data.get("speaking", False)
                    if is_speaking:
                        self.visualizer.set_state("speaking")
                        self.stop_button.set_state("active")
                    elif is_processing:
                        if "Escuchando" in self.input_field.placeholderText():
                            self.visualizer.set_state("listening")
                        else:
                            self.visualizer.set_state("thinking")
                        self.stop_button.set_state("active")
                    else:
                        self.visualizer.set_state("inactive")
                        self.stop_button.set_state("inactive")
                    
                    # Actualizar píldoras (Status Pills)
                    if litert_connected:
                        litert_backend = data.get("litert_backend", "CPU")
                        self.model_pill.setText(f"🧠 LiteRT: {litert_backend}")
                        if litert_backend == "GPU":
                            self.model_pill.setStyleSheet("""
                                QPushButton {
                                    background-color: rgba(203, 166, 247, 20);
                                    border: 1px solid rgba(203, 166, 247, 100);
                                    border-radius: 10px;
                                    color: #cba6f7;
                                    font-family: 'Inter';
                                    font-size: 10px;
                                    padding: 2px 8px;
                                }
                            """)
                        else:
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
                    
                    # Accel Pill (GPU/CPU verídico)
                    gpu_active = data.get("gpu_active", False)
                    if gpu_active:
                        self.accel_pill.setText("⚡ GPU: Activa")
                        self.accel_pill.setStyleSheet("""
                            QPushButton {
                                background-color: rgba(250, 179, 135, 20);
                                border: 1px solid rgba(250, 179, 135, 100);
                                border-radius: 10px;
                                color: #fab387;
                                font-family: 'Inter';
                                font-size: 10px;
                                padding: 2px 8px;
                            }
                        """)
                    else:
                        self.accel_pill.setText("⚙️ CPU: Activa")
                        self.accel_pill.setStyleSheet("""
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
                        if not self.isHidden() and not self.pending_gui_request and not self._resetting:
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
            self.accel_pill.setText("⚙️ Desconectado")
            self.accel_pill.setStyleSheet("""
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
            
            # Botón Stop Desconectado
            self.stop_button.set_state("offline")


    async def update_last_response(self):
        """Muestra el historial completo de la conversación."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("http://127.0.0.1:8765/history")
                if response.status_code == 200:
                    data = response.json()
                    history = data.get("history", [])
                    if not history:
                        return

                    parts = []
                    for msg in history:
                        role = msg.get("role", "")
                        content = msg.get("content", "")
                        if role == "user":
                            parts.append(f"**Tú:** {content}")
                        elif role == "assistant":
                            parts.append(f"**AsistenteIA:** {content}")
                        parts.append("---")

                    full_chat = "\n\n".join(parts)

                    self.animate_height(480)
                    self.chat_area.show()
                    self.chat_area.setMarkdown(full_chat)
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

        # Generar un ID de época único para esta petición
        self.current_request_id = getattr(self, 'current_request_id', 0) + 1
        req_id = self.current_request_id

        self.pending_gui_request = True
        self.input_field.setEnabled(False)
        self.input_field.setPlaceholderText("Pensando...")
        self.animate_height(480)
        self.chat_area.show()
        
        self.visualizer.set_state("thinking")
        
        # Activar el botón de Stop inmediatamente (feedback visual instantáneo)
        self.stop_button.set_state("active")
        
        # Mostrar lo que el usuario escribe inmediatamente
        self.chat_area.setMarkdown(f"**Tú:** {text}\n\n---\n\n**AsistenteIA:** ...")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", "http://127.0.0.1:8765/transcribe/stream", json={"text": text}) as response:
                    if response.status_code == 200:
                        first_chunk = True
                        accumulated_response = ""
                        async for chunk in response.aiter_text():
                            # Abortar el procesamiento si la época ha cambiado o se canceló
                            if not self.pending_gui_request or self.current_request_id != req_id:
                                break
                                
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
                        if self.current_request_id == req_id:
                            self.chat_area.setPlainText(f"Error: {response.status_code}")
        except Exception as e:
            if self.current_request_id == req_id:
                self.chat_area.setPlainText(f"Error de conexión: {e}")
        finally:
            # Solo limpiar o reactivar si somos la misma época
            if self.current_request_id == req_id:
                self.pending_gui_request = False
                self.input_field.setEnabled(True)
                self.input_field.clear()
                self.input_field.setFocus()

    def keyPressEvent(self, event: QKeyEvent):
        # ESC para cerrar panel
        if event.key() == Qt.Key_Escape:
            self.hide()
                
        # Ctrl+R para Reiniciar
        elif event.key() == Qt.Key_R and event.modifiers() & Qt.ControlModifier:
            asyncio.create_task(self.on_reset())
            event.accept()
            return
            
        # Ctrl+H para consultar Historial manual
        elif event.key() == Qt.Key_H and event.modifiers() & Qt.ControlModifier:
            if self._resetting:
                event.accept()
                return
            asyncio.create_task(self.update_last_response())
            event.accept()
            return
            
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
