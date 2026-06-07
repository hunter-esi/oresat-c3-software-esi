"""Antennas subsystem."""

from contextlib import suppress
from time import sleep

from olaf import GPIO_IN, GPIO_OUT, Adc, Gpio, logger

from ..drivers.max7310 import Max7310, Max7310Error, MockMax7310


class AntennasC3v6:
    """Antennas subsystem for the C3 v6 series.

    The v6 series cards uses GPIO lines off of the C3 board to deploy antennas. There are two
    antennas, the monopol and the helical, on opposite ends of the satellite.
    """

    def __init__(self, mock: bool = False):
        """
        Parameters
        ----------
        mock: bool
            Mock the hardware.
        """

        self._gpio_monopole_1 = Gpio("FIRE_ANTENNAS_1", mock)
        self._gpio_monopole_2 = Gpio("FIRE_ANTENNAS_2", mock)
        self._gpio_helical_1 = Gpio("FIRE_HELICAL_1", mock)
        self._gpio_helical_2 = Gpio("FIRE_HELICAL_2", mock)

        self._gpio_test_monopole = Gpio("TEST_ANTENNAS", mock)
        self._gpio_test_helical = Gpio("TEST_HELICAL", mock)

        self._adc_monopole = Adc(4, mock)
        self._adc_helical = Adc(5, mock)

    def deploy(self, timeout: int, delay_between: int):
        """
        Deploy the monopole antenna and then the helical.

        Wrapper ontop of deploy_monopole and deploy_helical.

        Parameters
        ----------
        timeout: int
            How long the gpio lines are set high.
        delay_between: int
            Delay between the monopole and helical deployments.
        """

        self.deploy_monopole(timeout)
        sleep(delay_between)
        self.deploy_helical(timeout)

    def deploy_helical(self, timeout: int):
        """
        Deploy only the helical.

        Parameters
        ----------
        timeout: int
            How long the gpio lines are set high.
        """

        self._gpio_helical_1.mode = GPIO_OUT
        self._gpio_helical_2.mode = GPIO_OUT

        self._gpio_helical_1.high()
        self._gpio_helical_2.high()
        sleep(timeout)
        self._gpio_helical_1.low()
        self._gpio_helical_2.low()

        self._gpio_helical_1.mode = GPIO_IN
        self._gpio_helical_2.mode = GPIO_IN

    def deploy_monopole(self, timeout: int):
        """
        Deploy only the monopole.

        Parameters
        ----------
        timeout: int
            How long the gpio lines are set high.
        """

        self._gpio_monopole_1.mode = GPIO_OUT
        self._gpio_monopole_2.mode = GPIO_OUT

        self._gpio_monopole_1.high()
        self._gpio_monopole_2.high()
        sleep(timeout)
        self._gpio_monopole_1.low()
        self._gpio_monopole_2.low()

        self._gpio_monopole_1.mode = GPIO_IN
        self._gpio_monopole_2.mode = GPIO_IN

    def is_helical_good(self, good_threshold: int) -> bool:
        """
        Test the helical resistor.

        Parameters
        ----------
        good_threshold: int
            The good threshold (anything above this value is good) in millivolts for
            testing an antenna.

        Returns
        -------
        bool
            Helical is good.
        """

        self._gpio_test_helical.high()
        value = self._adc_helical.value
        self._gpio_test_helical.low()
        return value >= good_threshold

    def is_monopole_good(self, good_threshold: int) -> bool:
        """
        Test the monopole resistor.

        Parameters
        ----------
        good_threshold: int
            The good threshold (anything above this value is good) in millivolts for
            testing an antenna.

        Returns
        -------
        bool
            Monopole is good.
        """

        self._gpio_test_monopole.high()
        value = self._adc_monopole.value
        self._gpio_test_monopole.low()
        return value >= good_threshold


class AntennasC3v7:
    """Antennas subsystem for v7 series C3 cards.

    The v7 series uses the OPD to deploy the antennas.
    """

    _READ_ANT_PIN = 0
    _FIRE_ANT_1_PIN = 1
    _FIRE_ANT_2_PIN = 2
    _TEST_ANT_PIN = 3
    _LIVE_INPUTS = (1 << _READ_ANT_PIN) | (1 << _TEST_ANT_PIN)
    _SAFE_INPUTS = _LIVE_INPUTS | (1 << _FIRE_ANT_1_PIN) | (1 << _FIRE_ANT_2_PIN)

    # FIXME: i2c_bus_num is reimplementation of whats in node_manager. Bad and should be fixed.
    def __init__(self, mock: bool = False, i2c_bus_num: int = 2) -> None:
        """
        Parameters
        ----------
        mock: bool
            Mock the hardware.
        i2c_bus_num:
            The /dev/i2c-n bus number to use.
        """
        self._I2C_BUS_NUM = i2c_bus_num
        if not mock:
            self._pz_end = Max7310(i2c_bus_num, 0x14)
            self._mz_end = Max7310(i2c_bus_num, 0x15)
            self._mz_mid = Max7310(i2c_bus_num, 0x16)
        else:
            self._pz_end = MockMax7310(i2c_bus_num, 0x14, 0)
            self._mz_end = MockMax7310(i2c_bus_num, 0x15, 0)
            self._mz_mid = MockMax7310(i2c_bus_num, 0x16, 0)

    def deploy(self, timeout: int, delay_between: int) -> None:
        """
        Deploy the plus z endcard (helical), then the minus z endcard (monopole), then the
        minus z midcard (ESI deployable solar wing).

        Parameters
        ----------
        timeout: int
            How long the gpio lines are set high.
        delay_between: int
            Delay between the monopole and helical deployments.
        """
        logger.info("Attempting minus z end card firing.")
        self._deploy_card(self._mz_end, timeout, "minuz z end")
        sleep(delay_between)
        logger.info("Attempting pos z end card firing.")
        self._deploy_card(self._pz_end, timeout, "pos z end")
        sleep(delay_between)
        logger.info("Attempting minus z mid card firing.")
        self._deploy_card(self._mz_mid, timeout, "minus z mid")

    def _deploy_card(self, card: Max7310, timeout: int, name: str) -> None:
        """
        Try to deploy the given card antenna.

        Parameters
        ----------
        timeout: int
            How long the gpio lines are set high.
        name:
            The name of the card being deployed.
        """
        fire = (1 << self._FIRE_ANT_1_PIN) | (1 << self._FIRE_ANT_2_PIN)
        try:
            card.configure(configuration=self._LIVE_INPUTS, polarity_inversion=0, output_port=fire)
            sleep(timeout)
            card.output_clear(self._FIRE_ANT_1_PIN, self._FIRE_ANT_2_PIN)
        except Max7310Error as e:
            logger.error(f"Deploy: {e}")
            logger.info(f"Tried and failed to fire {name} card deployer.")

        # Unconditionally try to safe the antenna pins
        with suppress(Max7310Error):
            card.configuration = self._SAFE_INPUTS
