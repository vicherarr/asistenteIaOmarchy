//! Cuándo se considera dicha la palabra.
//!
//! El modelo entrega una probabilidad cada pocas decenas de milisegundos.
//! Disparar con la primera que pase del umbral es el camino corto a los falsos
//! positivos: un pico aislado lo produce cualquier golpe. Lo que se hace en su
//! lugar es exigir que la **media de una ventana** se mantenga por encima del
//! umbral, que es la misma política que usan los satélites de ESPHome.
//!
//! Todo esto es aritmética pura, sin hardware ni FFI: se prueba en el PC.

/// Probabilidades que entran en la media móvil.
///
/// Con `stride` 3 sale una probabilidad cada 30 ms, así que 5 son ~150 ms:
/// suficiente para que un pico suelto no arrastre la media, y corto como para
/// no añadir retardo perceptible.
pub const VENTANA: usize = 5;

/// Inferencias a ignorar tras arrancar o reiniciar el detector.
///
/// El modelo en streaming necesita "llenarse" antes de que su salida
/// signifique algo; si se le hace caso desde la primera, dispara solo al
/// encender. 100 inferencias son ~3 s.
pub const INFERENCIAS_DE_GRACIA: u16 = 100;

/// Silencio impuesto tras un disparo, en inferencias (~1,5 s con stride 3).
///
/// Sin esto, una palabra reconocida produce varios disparos seguidos según la
/// media va bajando, y la máquina de estados recibe tres "despierta" para una
/// sola vez que has hablado.
pub const REFRACTARIO: u16 = 50;

/// Umbral por defecto, sobre 255.
///
/// Alto a propósito. "Luka" son dos sílabas cortas y de poca energía: es de la
/// clase de palabras que dispara sola. Se prefiere que al principio cueste y
/// bajarlo con el modo calibración, antes que empezar con un detector que se
/// activa con la tele y que acabes desenchufando la placa.
pub const UMBRAL_POR_DEFECTO: u8 = 200;

/// Media móvil + refractario sobre las probabilidades del modelo.
pub struct Politica {
    umbral: u8,
    ventana: [u8; VENTANA],
    siguiente: usize,
    /// Cuántas inferencias llevamos; mientras sea baja no se hace caso.
    vistas: u16,
    espera: u16,
    /// Con cuánta confianza se disparó la última vez.
    ///
    /// Se guarda porque [`Politica::empujar`] **limpia la ventana al disparar**,
    /// así que preguntar [`Politica::confianza`] después siempre da 0. Y es
    /// justo el número que hace falta para ajustar el umbral: cuánto margen
    /// había de verdad sobre él.
    confianza_disparo: u8,
}

impl Default for Politica {
    fn default() -> Self {
        Self::new(UMBRAL_POR_DEFECTO)
    }
}

impl Politica {
    pub fn new(umbral: u8) -> Self {
        Self { umbral, ventana: [0; VENTANA], siguiente: 0, vistas: 0, espera: 0, confianza_disparo: 0 }
    }

    /// Cambia el umbral en caliente, sin reiniciar el detector.
    pub fn set_umbral(&mut self, umbral: u8) {
        self.umbral = umbral;
    }

    pub fn umbral(&self) -> u8 {
        self.umbral
    }

    /// Vuelve al estado de recién arrancado. Se llama al salir de reposo.
    pub fn reset(&mut self) {
        self.ventana = [0; VENTANA];
        self.siguiente = 0;
        self.vistas = 0;
        self.espera = 0;
        self.confianza_disparo = 0;
    }

    /// Si se acaba de disparar y aún corre el refractario.
    ///
    /// Importa para medir, no para decidir: al disparar se limpia la ventana,
    /// pero las probabilidades altas que quedan de **esa misma palabra** la
    /// vuelven a llenar enseguida, así que la confianza sube otra vez sin que
    /// nadie haya dicho nada nuevo. Quien esté registrando picos tiene que
    /// saltarse este tramo o apuntará el rebote de un disparo como si fuera un
    /// evento aparte —que es exactamente lo que ensució la primera medición de
    /// barge-in y estuvo a punto de descartar la función entera.
    pub fn en_refractario(&self) -> bool {
        self.espera > 0
    }

    /// Si el detector aún está en el periodo de gracia y por tanto **no puede
    /// disparar pase lo que pase**.
    ///
    /// Lo mira la calibración: durante estos ~3 s la confianza sube y baja como
    /// siempre, y sin distinguirlo el log enseña "casi" que no lo eran —
    /// confianzas muy por encima del umbral que no despertaron por el arranque,
    /// no por falta de nivel. Eso lleva a bajar el umbral sin motivo.
    pub fn calentando(&self) -> bool {
        self.vistas <= INFERENCIAS_DE_GRACIA
    }

    /// Confianza que produjo el último disparo, 0 si aún no ha habido ninguno.
    ///
    /// Lo que hay que mirar para calibrar: si los disparos reales rondan el
    /// umbral, está demasiado alto y se escaparán la mitad de las veces.
    pub fn confianza_disparo(&self) -> u8 {
        self.confianza_disparo
    }

    /// Media de la ventana, 0-255. Es lo que alimenta el modo calibración.
    pub fn confianza(&self) -> u8 {
        let suma: u32 = self.ventana.iter().map(|&p| p as u32).sum();
        (suma / VENTANA as u32) as u8
    }

    /// Mete una probabilidad y dice si con ella se da por dicha la palabra.
    pub fn empujar(&mut self, probabilidad: u8) -> bool {
        self.ventana[self.siguiente] = probabilidad;
        self.siguiente = (self.siguiente + 1) % VENTANA;
        self.vistas = self.vistas.saturating_add(1);

        if self.espera > 0 {
            self.espera -= 1;
            return false;
        }
        // `<=` y no `<`: `vistas` ya cuenta la de ahora, y la número 100
        // todavía forma parte de las 100 de gracia.
        if self.vistas <= INFERENCIAS_DE_GRACIA {
            return false;
        }
        let confianza = self.confianza();
        if confianza <= self.umbral {
            return false;
        }

        // Se anota ANTES de limpiar, que es la única ventana en la que existe.
        self.confianza_disparo = confianza;
        // Al disparar se limpia la ventana: si no, las siguientes inferencias
        // seguirían viendo la media alta y el refractario solo taparía el
        // problema en vez de resolverlo.
        self.ventana = [0; VENTANA];
        self.espera = REFRACTARIO;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Lleva la política más allá del periodo de gracia con silencio.
    fn calentar(p: &mut Politica) {
        for _ in 0..INFERENCIAS_DE_GRACIA {
            assert!(!p.empujar(0));
        }
    }

    #[test]
    fn no_dispara_durante_la_gracia() {
        let mut p = Politica::new(100);
        // Aunque el modelo esté completamente seguro desde el primer momento.
        for _ in 0..INFERENCIAS_DE_GRACIA {
            assert!(!p.empujar(255));
        }
    }

    #[test]
    fn dispara_con_probabilidades_altas_sostenidas() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        let mut disparos = 0;
        for _ in 0..VENTANA {
            if p.empujar(255) {
                disparos += 1;
            }
        }
        assert_eq!(disparos, 1, "debe disparar una sola vez");
    }

    #[test]
    fn un_pico_aislado_no_dispara() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        // Un solo 255 entre ceros deja la media en 51, por debajo de 100.
        assert!(!p.empujar(255));
        for _ in 0..VENTANA {
            assert!(!p.empujar(0));
        }
    }

    #[test]
    fn el_refractario_impide_disparos_encadenados() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        while !p.empujar(255) {}
        // Con la palabra aún sonando, no debe volver a disparar.
        for _ in 0..REFRACTARIO {
            assert!(!p.empujar(255));
        }
    }

    #[test]
    fn tras_el_refractario_vuelve_a_disparar() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        while !p.empujar(255) {}
        for _ in 0..REFRACTARIO {
            p.empujar(0);
        }
        let mut disparo = false;
        for _ in 0..VENTANA {
            disparo |= p.empujar(255);
        }
        assert!(disparo, "una segunda palabra debe poder despertar");
    }

    #[test]
    fn el_umbral_alto_rechaza_lo_que_el_bajo_acepta() {
        let mut permisiva = Politica::new(100);
        let mut estricta = Politica::new(230);
        calentar(&mut permisiva);
        calentar(&mut estricta);

        let mut acepta = false;
        let mut rechaza = true;
        for _ in 0..VENTANA * 2 {
            acepta |= permisiva.empujar(180);
            rechaza &= !estricta.empujar(180);
        }
        assert!(acepta && rechaza);
    }

    #[test]
    fn reset_vuelve_a_exigir_el_periodo_de_gracia() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        p.reset();
        for _ in 0..INFERENCIAS_DE_GRACIA {
            assert!(!p.empujar(255));
        }
    }

    /// La regresión que se coló hasta la placa: el log de disparo consultaba
    /// `confianza()` DESPUÉS de disparar, y como `empujar` limpia la ventana,
    /// imprimía un 0 fijo. El número real hay que capturarlo antes.
    #[test]
    fn el_disparo_recuerda_su_confianza_aunque_la_ventana_se_limpie() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        while !p.empujar(200) {}

        assert_eq!(p.confianza(), 0, "la ventana debe quedar limpia tras disparar");
        assert!(
            p.confianza_disparo() > p.umbral(),
            "el disparo tuvo que superar el umbral: {} vs {}",
            p.confianza_disparo(),
            p.umbral()
        );
        assert!(p.confianza_disparo() <= 200, "no puede superar la probabilidad de entrada");
    }

    #[test]
    fn sin_disparos_la_confianza_de_disparo_es_cero() {
        let mut p = Politica::new(100);
        calentar(&mut p);
        assert_eq!(p.confianza_disparo(), 0);
    }

    #[test]
    fn la_confianza_refleja_la_media() {
        let mut p = Politica::default();
        for _ in 0..VENTANA {
            p.empujar(200);
        }
        assert_eq!(p.confianza(), 200);
    }
}
