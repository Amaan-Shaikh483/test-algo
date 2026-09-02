export default function ({ registerIndicator, sourceValues }) {
  registerIndicator({
    id: 'piner-test',
    name: 'Piner Test',
    category: 'Custom',
    placement: 'onchart',

    inputs: [
      {
        key: 'length',
        type: 'number',
        label: 'Length',
        default: 9,
        min: 1,
        step: 1
      }
    ],

    plots: [
      {
        key: 'sma',
        type: 'line',
        title: 'SMA'
      }
    ],

    calc(bars, settings) {
      const length = Number(settings.length);
      const sma = new Array(bars.length).fill(null);

      let sum = 0;

      for (let i = 0; i < bars.length; i++) {
        sum += bars[i].close;

        if (i >= length) {
          sum -= bars[i - length].close;
        }

        if (i >= length - 1) {
          sma[i] = sum / length;
        }
      }

      return { sma };
    }
  });
}