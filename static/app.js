/* NSE Data Viewer - static app script
   Reads page config from window.NSE_CONFIG (injected by the template). */
(function () {
    'use strict';

    var CONFIG = window.NSE_CONFIG || {};
    var currentSymbol = CONFIG.symbol || 'RELIANCE';
    var hasSymbol = !!CONFIG.symbol;
    var activeTab = CONFIG.activeTab || 'charts';

    // ---------------- Market-hours awareness (IST) ----------------
    function istNow() {
        var parts = new Date().toLocaleString('en-US', { hour12: false, timeZone: 'Asia/Kolkata' });
        var d = new Date(parts);
        return d;
    }

    function isMarketOpen() {
        var d = istNow();
        var day = d.getDay();
        if (day === 0 || day === 6) return false;
        var minutes = d.getHours() * 60 + d.getMinutes();
        return minutes >= 9 * 60 && minutes <= 15 * 60 + 30;
    }

    function getPollMs(openMs, closedMs) {
        return isMarketOpen() ? openMs : closedMs;
    }

    // ---------------- Historical tab ----------------
    function setPeriod(days) {
        var toDate = new Date();
        var fromDate = new Date();
        fromDate.setDate(toDate.getDate() - days);

        document.querySelector('#historicalForm input[name="from_date"]').value = fromDate.toISOString().split('T')[0];
        document.querySelector('#historicalForm input[name="to_date"]').value = toDate.toISOString().split('T')[0];
        loadHistorical();
    }

    function showHistoricalStatus(text, isError) {
        var status = document.getElementById('historicalStatus');
        var statusText = document.getElementById('historicalStatusText');
        var empty = document.getElementById('historicalEmpty');
        var table = document.getElementById('historicalTable');
        if (status) status.classList.remove('d-none');
        if (statusText) statusText.textContent = text;
        if (status) status.className = 'alert ' + (isError ? 'alert-danger' : 'alert-info') + ' py-2 d-none';
        if (status) status.classList.remove('d-none');
        if (empty) empty.classList.add('d-none');
        if (table) table.classList.add('d-none');
    }

    function loadHistorical() {
        var form = document.getElementById('historicalForm');
        if (!form) return;
        var symbol = form.querySelector('input[name="symbol"]').value.trim().toUpperCase();
        var fromDate = form.querySelector('input[name="from_date"]').value;
        var toDate = form.querySelector('input[name="to_date"]').value;
        if (!symbol || !fromDate || !toDate) return;

        showHistoricalStatus('Loading historical data...', false);

        fetch('/get_historical?symbol=' + encodeURIComponent(symbol) +
            '&from_date=' + encodeURIComponent(fromDate) +
            '&to_date=' + encodeURIComponent(toDate))
            .then(function (response) { return response.json(); })
            .then(function (data) {
                var status = document.getElementById('historicalStatus');
                var empty = document.getElementById('historicalEmpty');
                var table = document.getElementById('historicalTable');
                var tbody = document.getElementById('historicalBody');

                if (status) status.classList.add('d-none');
                if (data.error) {
                    if (status) {
                        status.className = 'alert alert-danger py-2';
                        status.classList.remove('d-none');
                        document.getElementById('historicalStatusText').textContent = data.error;
                    }
                    if (table) table.classList.add('d-none');
                    if (empty) empty.classList.remove('d-none');
                    return;
                }
                if (!Array.isArray(data) || data.length === 0) {
                    if (table) table.classList.add('d-none');
                    if (empty) {
                        empty.classList.remove('d-none');
                        empty.innerHTML = '<i class="bi bi-info-circle me-1"></i>No historical data found for the selected range.';
                    }
                    return;
                }

                if (empty) empty.classList.add('d-none');

                var rows = data.map(function (s) {
                    var cr = function (v) { return (v ? (v / 10000000).toFixed(2) : 'N/A'); };
                    var num = function (v) { return (v !== null && v !== undefined ? Number(v).toLocaleString('en-IN') : 'N/A'); };
                    return '<tr>' +
                        '<td>' + s.date + '</td>' +
                        '<td class="text-end">' + num(s.open) + '</td>' +
                        '<td class="text-end">' + num(s.high) + '</td>' +
                        '<td class="text-end">' + num(s.low) + '</td>' +
                        '<td class="text-end fw-semibold">' + num(s.close) + '</td>' +
                        '<td class="text-end">' + num(s.last_traded) + '</td>' +
                        '<td class="text-end">' + num(s.previous_close) + '</td>' +
                        '<td class="text-end">' + num(s.volume) + '</td>' +
                        '<td class="text-end">' + cr(s.value) + '</td>' +
                        '<td class="text-end">' + num(s['52w_high']) + '</td>' +
                        '<td class="text-end">' + num(s['52w_low']) + '</td>' +
                        '<td class="text-end">' + num(s.total_trades) + '</td>' +
                        '<td class="text-end">' + num(s.vwap) + '</td>' +
                        '</tr>';
                }).join('');

                tbody.innerHTML = rows;
                if (table) table.classList.remove('d-none');
            })
            .catch(function (error) {
                console.error('Error loading historical data:', error);
                showHistoricalStatus('Error loading historical data. Please try again.', true);
            });
    }

    // ---------------- Peers & Sector tab ----------------
    function showPeersStatus(text, isError) {
        var status = document.getElementById('peersStatus');
        var statusText = document.getElementById('peersStatusText');
        var empty = document.getElementById('peersEmpty');
        var table = document.getElementById('peersTable');
        if (status) {
            status.className = 'alert ' + (isError ? 'alert-danger' : 'alert-info') + ' py-2';
            status.classList.remove('d-none');
        }
        if (statusText) statusText.textContent = text;
        if (empty) empty.classList.add('d-none');
        if (table) table.classList.add('d-none');
    }

    function loadPeers() {
        showPeersStatus('Loading peer data...', false);

        fetch('/get_peers?symbol=' + encodeURIComponent(currentSymbol))
            .then(function (response) { return response.json(); })
            .then(function (data) {
                var status = document.getElementById('peersStatus');
                var empty = document.getElementById('peersEmpty');
                var emptyText = document.getElementById('peersEmptyText');
                var table = document.getElementById('peersTable');
                var tbody = document.getElementById('peersBody');

                if (status) status.classList.add('d-none');

                if (data.error) {
                    showPeersStatus(data.error, true);
                    return;
                }

                var dropEl = function (id, value) {
                    var el = document.getElementById(id);
                    if (el) el.textContent = value;
                };

                var sector = data.sector || {};
                dropEl('peerIndustry', sector.industry || '-');
                dropEl('peerSectorIndex', sector.sectorIndex || '-');
                dropEl('peerSectorPe', sector.sectorPe || '-');
                dropEl('peerCount', (data.peers || []).length);

                if (!data.peers || data.peers.length === 0) {
                    if (table) table.classList.add('d-none');
                    if (emptyText) emptyText.textContent = data.message ||
                        'No peer data available for ' + currentSymbol + '.';
                    if (empty) empty.classList.remove('d-none');
                    return;
                }

                var fmt = function (v, dec) {
                    return (v !== null && v !== undefined && !isNaN(v))
                        ? Number(v).toFixed(dec || 2) : '-';
                };

                var rows = data.peers.map(function (p) {
                    var change = (p.change !== null && p.change !== undefined) ? p.change : 0;
                    var pChange = (p.pChange !== null && p.pChange !== undefined) ? p.pChange : 0;
                    var cls = change >= 0 ? 'green-text' : 'red-text';
                    var sign = change >= 0 ? '+' : '-';

                    return '<tr>' +
                        '<td class="fw-semibold">' +
                        '<a href="/?symbol=' + encodeURIComponent(p.symbol) +
                        '" class="text-decoration-none">' + p.symbol + '</a></td>' +
                        '<td>' + (p.name || '-') + '</td>' +
                        '<td class="text-end">' + fmt(p.lastPrice) + '</td>' +
                        '<td class="text-end ' + cls + '">' + (change ? sign + Math.abs(change).toFixed(2) : '-') + '</td>' +
                        '<td class="text-end ' + cls + '">' + (pChange ? sign + Math.abs(pChange).toFixed(2) + '%' : '-') + '</td>' +
                        '<td class="text-end">' + (p.pe || '-') + '</td>' +
                        '<td>' + (p.industry || '-') + '</td>' +
                        '</tr>';
                }).join('');

                tbody.innerHTML = rows;
                if (empty) empty.classList.add('d-none');
                if (table) table.classList.remove('d-none');
            })
            .catch(function (error) {
                console.error('Error loading peers:', error);
                showPeersStatus('Error loading peer data. Please try again.', true);
            });
    }

    // ---------------- Indices Slider ----------------
    var indicesUpdateInterval = null;

    function scheduleIndices() {
        clearInterval(indicesUpdateInterval);
        indicesUpdateInterval = setInterval(fetchIndicesData, getPollMs(15000, 60000));
    }

    function initializeIndicesSlider() {
        fetchIndicesData();
        scheduleIndices();
    }

    function fetchIndicesData() {
        fetch('/get_indices_data')
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP error! status: ' + response.status);
                return response.json();
            })
            .then(function (data) {
                if (Array.isArray(data)) {
                    if ($('#indicesSlider').hasClass('slick-initialized')) {
                        updateIndicesSlider(data);
                    } else {
                        initIndicesSlider(data);
                    }
                    document.getElementById('indicesLastUpdated').textContent =
                        'Last updated: ' + new Date().toLocaleTimeString();
                }
            })
            .catch(function (error) {
                console.error('Error fetching indices:', error);
                $('#indicesSlider').html('<div class="text-muted p-2">Error loading indices data</div>');
            });
    }

    function updateIndicesSlider(data) {
        if (!data || data.length === 0) return;

        $('#indicesSlider .slick-slide:not(.slick-cloned)').each(function (index) {
            if (index < data.length) {
                var indexData = data[index];
                var isPositive = indexData.change >= 0;
                var changeClass = isPositive ? 'green_box' : 'red_box';
                var changeSign = isPositive ? '+' : '-';
                var percentSign = isPositive ? '+' : '-';

                $(this).html(
                    '<div class="item indices_details text-start ' + changeClass + '" style="padding:10px;margin-bottom:10px">' +
                    '<p class="symbol">' + (indexData.symbol || 'N/A') + '</p>' +
                    '<h3 class="value">' + (indexData.last ? indexData.last.toFixed(2) : 'N/A') + '</h3>' +
                    '<p class="chng">' +
                    changeSign + Math.abs(indexData.change).toFixed(2) +
                    ' (' + percentSign + Math.abs(indexData.change_percent).toFixed(2) + '%)' +
                    '</p>' +
                    '</div>'
                );
            }
        });
    }

    function initIndicesSlider(data) {
        var slider = $('#indicesSlider');
        slider.empty();

        data.forEach(function (index) {
            var isPositive = index.change >= 0;
            var changeClass = isPositive ? 'green_box' : 'red_box';
            var changeSign = isPositive ? '+' : '-';
            var percentSign = isPositive ? '+' : '-';

            slider.append(
                '<div>' +
                '<div class="item indices_details text-start ' + changeClass + '" style="padding:10px;margin-bottom:10px">' +
                '<p class="symbol">' + (index.symbol || 'N/A') + '</p>' +
                '<h3 class="value">' + (index.last ? index.last.toFixed(2) : 'N/A') + '</h3>' +
                '<p class="chng">' +
                changeSign + Math.abs(index.change).toFixed(2) +
                ' (' + percentSign + Math.abs(index.change_percent).toFixed(2) + '%)' +
                '</p>' +
                '</div>' +
                '</div>'
            );
        });

        slider.slick({
            slidesToShow: 6,
            slidesToScroll: 6,
            autoplay: false,
            arrows: true,
            infinite: false,
            responsive: [
                { breakpoint: 1200, settings: { slidesToShow: 5, slidesToScroll: 5 } },
                { breakpoint: 992, settings: { slidesToShow: 4, slidesToScroll: 4 } },
                { breakpoint: 768, settings: { slidesToShow: 3, slidesToScroll: 3 } },
                { breakpoint: 576, settings: { slidesToShow: 2, slidesToScroll: 2 } }
            ]
        });
    }

    // ---------------- Stock Data Handling ----------------
    var stockUpdateInterval = null;
    var prevClosePrice = 0;

    function showErrorToUser(message) {
        var tile = document.getElementById('mainPriceTile');
        var lastPriceEl = document.getElementById('lastPrice');
        var changeValueEl = document.getElementById('changeValue');
        var changePercentEl = document.getElementById('changePercent');
        var stockSymbolEl = document.getElementById('stockSymbol');

        if (tile) tile.classList.add('red_box');
        if (lastPriceEl) lastPriceEl.textContent = 'N/A';
        if (changeValueEl) changeValueEl.textContent = '-';
        if (changePercentEl) changePercentEl.textContent = '(-%)';
        if (stockSymbolEl) stockSymbolEl.textContent = currentSymbol;

        var err = document.getElementById('stockError');
        if (!err && tile) {
            err = document.createElement('div');
            err.id = 'stockError';
            err.className = 'alert alert-warning mt-2 mb-0 py-1 px-2 small';
            tile.closest('.card-body').appendChild(err);
        }
        if (err) err.textContent = 'Error loading data: ' + message + '. Will retry automatically.';
    }

    function scheduleStock() {
        clearInterval(stockUpdateInterval);
        stockUpdateInterval = setInterval(fetchStockData, getPollMs(5000, 60000));
    }

    function fetchStockData() {
        fetch('/get_stock_data?symbol=' + encodeURIComponent(currentSymbol))
            .then(function (response) {
                if (!response.ok) {
                    var retryAfter = parseInt(response.headers.get('Retry-After')) || 5;
                    console.warn('Server busy, retrying after ' + retryAfter + ' seconds');
                    setTimeout(fetchStockData, retryAfter * 1000);
                    throw new Error('HTTP error! status: ' + response.status);
                }
                return response.json();
            })
            .then(function (data) {
                if (data.error) {
                    showErrorToUser(data.error);
                    clearInterval(stockUpdateInterval);
                    stockUpdateInterval = setInterval(fetchStockData, 10000);
                } else {
                    updateStockData(data);
                    scheduleStock();
                }
            })
            .catch(function (error) {
                console.error('Error fetching stock data:', error);
            });
    }

    function updateStockData(data) {
        if (!data) return;

        var lastPrice = data.priceInfo.lastPrice;
        var change = data.priceInfo.change;
        var pChange = data.priceInfo.pChange;
        var isPositive = change >= 0;

        var dropEl = function (id, value) {
            var el = document.getElementById(id);
            if (el) el.textContent = value;
        };

        dropEl('stockSymbol', data.info.symbol);
        dropEl('companyName', data.info.companyName);
        dropEl('lastPrice', lastPrice.toFixed(2));
        dropEl('changeValue', change.toFixed(2));
        dropEl('changePercent', '(' + pChange.toFixed(2) + '%)');

        var changeIcon = document.getElementById('changeIcon');
        if (changeIcon) {
            changeIcon.className = isPositive ? 'bi bi-arrow-up-right' : 'bi bi-arrow-down-right';
        }

        var priceChangeElement = document.getElementById('priceChange');
        if (priceChangeElement) {
            priceChangeElement.className = isPositive ?
                'd-flex align-items-center justify-content-end green-text' :
                'd-flex align-items-center justify-content-end red-text';
        }

        var mainPriceTile = document.getElementById('mainPriceTile');
        if (mainPriceTile) {
            mainPriceTile.classList.remove('green_box', 'red_box');
            mainPriceTile.classList.add(isPositive ? 'green_box' : 'red_box');
        }

        dropEl('openPrice', data.priceInfo.open.toFixed(2));
        dropEl('prevClose', data.priceInfo.previousClose.toFixed(2));

        var newPrevClose = Number(data.priceInfo.previousClose) || 0;
        if (newPrevClose > 0 && newPrevClose !== prevClosePrice) {
            prevClosePrice = newPrevClose;
            updatePrevCloseAnnotation();
        }
        dropEl('dayHigh', data.priceInfo.intraDayHighLow.max.toFixed(2));
        dropEl('dayLow', data.priceInfo.intraDayHighLow.min.toFixed(2));
        dropEl('weekHigh', data.priceInfo.weekHighLow.max.toFixed(2));
        dropEl('weekLow', data.priceInfo.weekHighLow.min.toFixed(2));
        dropEl('upperLimit', data.priceInfo.upperCP);
        dropEl('lowerLimit', data.priceInfo.lowerCP);
        dropEl('peRatio', data.metadata.pdSectorPe || '-');
        dropEl('sectorIndex', data.metadata.pdSectorInd || '-');
        dropEl('industry', data.info.industry || '-');
        dropEl('marketLot', (data.securityInfo && data.securityInfo.marketLot) || '-');

        dropEl('stockLastUpdated', 'Last updated: ' + new Date().toLocaleTimeString());

        if (data.preOpenMarket) {
            var preOpen = data.preOpenMarket;
            dropEl('preOpenVolume', preOpen.totalTradedVolume.toLocaleString());
            dropEl('preOpenBuyQty', preOpen.totalBuyQuantity.toLocaleString());
            dropEl('preOpenSellQty', preOpen.totalSellQuantity.toLocaleString());

            var preOpenChange = preOpen.Change;
            var preOpenPerChange = preOpen.perChange;
            var isPreOpenPositive = preOpenChange >= 0;

            dropEl('preOpenChange', preOpenChange.toFixed(2));
            dropEl('preOpenPerChange', preOpenPerChange.toFixed(2) + '%');

            var pChangeEl = document.getElementById('preOpenChange');
            var pPerEl = document.getElementById('preOpenPerChange');
            pChangeEl.className = isPreOpenPositive ? 'mb-0 green-text' : 'mb-0 red-text';
            pPerEl.className = isPreOpenPositive ? 'mb-0 green-text' : 'mb-0 red-text';
        }

        if (data.marketDeptOrderBook && data.marketDeptOrderBook.tradeInfo) {
            var tradeInfo = data.marketDeptOrderBook.tradeInfo;

            dropEl('totalTradedVolume',
                tradeInfo.totalTradedVolume ? parseFloat(tradeInfo.totalTradedVolume).toLocaleString() : '-');
            dropEl('totalTradedValue',
                tradeInfo.totalTradedValue ? parseFloat(tradeInfo.totalTradedValue).toFixed(2) + ' Cr' : '-');
            dropEl('dailyVolatility',
                tradeInfo.cmDailyVolatility ? parseFloat(tradeInfo.cmDailyVolatility).toFixed(2) + '%' : '-');
            dropEl('annualVolatility',
                tradeInfo.cmAnnualVolatility ? parseFloat(tradeInfo.cmAnnualVolatility).toFixed(2) + '%' : '-');
        }

        if (data.marketDeptOrderBook && data.marketDeptOrderBook.valueAtRisk) {
            var valueAtRisk = data.marketDeptOrderBook.valueAtRisk;

            dropEl('securityVar',
                valueAtRisk.securityVar !== undefined ? parseFloat(valueAtRisk.securityVar).toFixed(2) + '%' : '-');
            dropEl('varMargin',
                valueAtRisk.varMargin !== undefined ? parseFloat(valueAtRisk.varMargin).toFixed(2) + '%' : '-');
            dropEl('extremeLossMargin',
                valueAtRisk.extremeLossMargin !== undefined ? parseFloat(valueAtRisk.extremeLossMargin).toFixed(2) + '%' : '-');
            dropEl('applicableMargin',
                valueAtRisk.applicableMargin !== undefined ? parseFloat(valueAtRisk.applicableMargin).toFixed(2) + '%' : '-');
        }

        if (data.securityWiseDP) {
            dropEl('deliveryPercentage',
                data.securityWiseDP.deliveryToTradedQuantity ?
                    parseFloat(data.securityWiseDP.deliveryToTradedQuantity).toFixed(2) + '%' : '-');
        }
    }

    function initializeStockData() {
        fetchStockData();
        scheduleStock();
    }

    // ---------------- Search Suggestions ----------------
    var LS_KEY = 'nseLastSymbol';

    function rememberSymbol(symbol) {
        try { localStorage.setItem(LS_KEY, symbol); } catch (e) { }
    }

    function submitSearch() {
        var input = document.getElementById('stockSearch');
        if (input && input.value.trim()) {
            rememberSymbol(input.value.trim().toUpperCase());
        }
        document.getElementById('dataForm').submit();
    }

    function initSearch() {
        var searchInput = document.getElementById('stockSearch');
        var suggestionsBox = document.getElementById('searchSuggestions');
        if (!searchInput || !suggestionsBox) return;
        var debounceTimer;
        var activeIndex = -1;

        searchInput.addEventListener('input', function () {
            clearTimeout(debounceTimer);
            var query = this.value.trim();

            if (query.length < 2) {
                suggestionsBox.style.display = 'none';
                return;
            }

            debounceTimer = setTimeout(function () {
                fetch('/search_stocks?query=' + encodeURIComponent(query))
                    .then(function (response) { return response.json(); })
                    .then(function (suggestions) {
                        if (suggestions.length > 0) {
                            suggestionsBox.innerHTML = '';
                            activeIndex = -1;
                            suggestions.forEach(function (stock) {
                                var suggestionItem = document.createElement('a');
                                suggestionItem.className = 'dropdown-item';
                                suggestionItem.href = '#';
                                suggestionItem.innerHTML =
                                    '<strong>' + stock.symbol + '</strong> - ' + stock.name;
                                suggestionItem.addEventListener('click', function (e) {
                                    e.preventDefault();
                                    searchInput.value = stock.symbol;
                                    suggestionsBox.style.display = 'none';
                                    submitSearch();
                                });
                                suggestionsBox.appendChild(suggestionItem);
                            });
                            suggestionsBox.style.display = 'block';
                        } else {
                            suggestionsBox.style.display = 'none';
                        }
                    })
                    .catch(function (error) {
                        console.error('Error fetching suggestions:', error);
                        suggestionsBox.style.display = 'none';
                    });
            }, 300);
        });

        document.getElementById('dataForm').addEventListener('submit', function () {
            var input = document.getElementById('stockSearch');
            if (input && input.value.trim()) {
                rememberSymbol(input.value.trim().toUpperCase());
            }
        });

        document.addEventListener('click', function (e) {
            if (e.target !== searchInput && !suggestionsBox.contains(e.target)) {
                suggestionsBox.style.display = 'none';
            }
        });

        searchInput.addEventListener('keydown', function (e) {
            var items = suggestionsBox.querySelectorAll('.dropdown-item');
            if (items.length === 0) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (suggestionsBox.style.display === 'none') return;
                activeIndex = (activeIndex + 1) % items.length;
                items[activeIndex].focus();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (suggestionsBox.style.display === 'none') return;
                activeIndex = (activeIndex - 1 + items.length) % items.length;
                items[activeIndex].focus();
            } else if (e.key === 'Enter' && activeIndex >= 0 && document.activeElement === items[activeIndex]) {
                e.preventDefault();
                items[activeIndex].click();
            } else if (e.key === 'Escape') {
                suggestionsBox.style.display = 'none';
                activeIndex = -1;
            }
        });
    }

    // ---------------- Chart Handling ----------------
    var preOpenChartInstance = null;
    var normalMarketChartInstance = null;
    var lastPreOpenData = null;
    var lastNormalMarketData = null;

    function buildPrevCloseAnnotation() {
        if (!prevClosePrice) return {};
        return {
            prevCloseLine: {
                type: 'line',
                yMin: prevClosePrice,
                yMax: prevClosePrice,
                borderColor: 'rgba(255, 165, 0, 0.8)',
                borderWidth: 2,
                borderDash: [5, 5],
                label: {
                    enabled: true,
                    content: 'Prev Close: \u20B9' + prevClosePrice.toFixed(2),
                    position: 'start',
                    backgroundColor: 'rgba(255, 165, 0, 0.7)',
                    font: { size: 12 }
                }
            }
        };
    }

    function updatePrevCloseAnnotation() {
        [preOpenChartInstance, normalMarketChartInstance].forEach(function (chart) {
            if (chart && chart.options && chart.options.plugins) {
                chart.options.plugins.annotation.annotations = buildPrevCloseAnnotation();
                chart.update();
            }
        });
    }

    var chartConfig = {
        type: 'line',
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 250, easing: 'linear' },
            elements: {
                line: { tension: 0.4, borderWidth: 2 },
                point: { radius: 0, hoverRadius: 4 }
            },
            plugins: {
                legend: { display: true },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    callbacks: {
                        label: function (context) {
                            return context.dataset.label + ': \u20B9' + context.parsed.y.toFixed(2);
                        }
                    }
                },
                zoom: {
                    pan: { enabled: true, mode: 'xy' },
                    zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'xy' }
                },
                annotation: {
                    annotations: buildPrevCloseAnnotation()
                }
            },
            scales: {
                x: {
                    title: { display: true, text: 'Time' },
                    ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 20 }
                },
                y: {
                    title: { display: true, text: 'Price (\u20B9)' },
                    beginAtZero: false,
                    ticks: {
                        callback: function (value) { return '\u20B9' + value.toFixed(2); }
                    }
                }
            }
        }
    };

    function noChartData(state) {
        var el = document.getElementById('noChartData');
        if (el) el.classList.toggle('d-none', !state);
    }

    function yScaleMin(yMin) {
        return prevClosePrice > 0 ? Math.min(yMin, prevClosePrice * 0.99) : yMin;
    }

    function initializeCharts() {
        fetch('/get_live_data?symbol=' + encodeURIComponent(currentSymbol))
            .then(function (response) { return response.json(); })
            .then(function (data) {
                createChartsFromData(data);
                startLiveUpdates(currentSymbol);
            })
            .catch(function (error) {
                console.error('Error initializing charts:', error);
                noChartData(true);
            });
    }

    function createChartsFromData(dynamicChart) {
        if (!dynamicChart || dynamicChart.error) {
            noChartData(true);
            return;
        }

        var preOpenCanvas = document.getElementById('preOpenChart');
        if (preOpenCanvas && dynamicChart.pre_open && dynamicChart.pre_open.times.length > 0) {
            var preOpenPrices = dynamicChart.pre_open.prices;

            document.getElementById('preOpenChartContainer').classList.remove('d-none');
            var yMin = Math.min.apply(null, preOpenPrices.filter(function (p) { return p > 0; })) * 0.99;

            preOpenChartInstance = new Chart(preOpenCanvas.getContext('2d'), {
                ...chartConfig,
                data: {
                    labels: dynamicChart.pre_open.times,
                    datasets: [{
                        label: 'Pre-Open Price',
                        data: preOpenPrices,
                        borderColor: 'rgba(255, 99, 132, 1)',
                        backgroundColor: 'rgba(255, 99, 132, 0.1)',
                        fill: true
                    }]
                },
                options: {
                    ...chartConfig.options,
                    scales: {
                        ...chartConfig.options.scales,
                        y: { ...chartConfig.options.scales.y, min: yScaleMin(yMin) }
                    },
                    plugins: {
                        ...chartConfig.options.plugins,
                        title: { display: true, text: currentSymbol + ' Pre-Open Market Prices' }
                    }
                }
            });
            lastPreOpenData = dynamicChart.pre_open;
            document.getElementById('preOpenLastUpdated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
        }

        var normalMarketCanvas = document.getElementById('normalMarketChart');
        if (normalMarketCanvas && dynamicChart.normal_market && dynamicChart.normal_market.times.length > 0) {
            var normalPrices = dynamicChart.normal_market.prices;

            document.getElementById('normalMarketChartContainer').classList.remove('d-none');
            var yMin2 = Math.min.apply(null, normalPrices.filter(function (p) { return p > 0; })) * 0.99;

            normalMarketChartInstance = new Chart(normalMarketCanvas.getContext('2d'), {
                ...chartConfig,
                data: {
                    labels: dynamicChart.normal_market.times,
                    datasets: [{
                        label: 'Market Price',
                        data: normalPrices,
                        borderColor: 'rgba(54, 162, 235, 1)',
                        backgroundColor: 'rgba(54, 162, 235, 0.1)',
                        fill: true
                    }]
                },
                options: {
                    ...chartConfig.options,
                    scales: {
                        ...chartConfig.options.scales,
                        y: { ...chartConfig.options.scales.y, min: yScaleMin(yMin2) }
                    },
                    plugins: {
                        ...chartConfig.options.plugins,
                        title: {
                            display: true,
                            text: currentSymbol + ' Normal Market Prices'
                        }
                    }
                }
            });
            lastNormalMarketData = dynamicChart.normal_market;
            document.getElementById('normalMarketLastUpdated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
        }

        var closePrice = dynamicChart.close_price;
        if (closePrice) {
            var badge = document.getElementById('closePriceBadge');
            if (badge) {
                badge.textContent = 'Closing Price: \u20B9' + closePrice.toFixed(2);
                badge.classList.remove('d-none');
            }
        }

        if (!preOpenChartInstance && !normalMarketChartInstance) {
            noChartData(true);
        }
    }

    function startLiveUpdates(symbol) {
        var lastUpdateTime = 0;
        function updateLoop() {
            var currentTime = Date.now();
            var interval = getPollMs(4000, 60000);
            if (currentTime - lastUpdateTime >= interval) {
                updateLiveData(symbol);
                lastUpdateTime = currentTime;
            }
            requestAnimationFrame(updateLoop);
        }
        requestAnimationFrame(updateLoop);
    }

    function updateLiveData(symbol) {
        fetch('/get_live_data?symbol=' + encodeURIComponent(symbol))
            .then(function (response) { return response.json(); })
            .then(function (data) {
                var now = new Date().toLocaleTimeString();

                if (data.error) {
                    if (!preOpenChartInstance && !normalMarketChartInstance) noChartData(true);
                    return;
                }

                if (!preOpenChartInstance && data.pre_open && data.pre_open.times.length > 0) {
                    createChartsFromData(data);
                    return;
                }

                if (preOpenChartInstance && data.pre_open) {
                    smoothUpdateChart(
                        preOpenChartInstance,
                        data.pre_open,
                        lastPreOpenData,
                        currentSymbol + ' Pre-Open Market Prices | Latest: \u20B9' + data.pre_open.prices.slice(-1)[0].toFixed(2)
                    );
                    lastPreOpenData = data.pre_open;
                    document.getElementById('preOpenLastUpdated').textContent = 'Last updated: ' + now;
                }

                if (normalMarketChartInstance && data.normal_market) {
                    var title = currentSymbol + ' Normal Market Prices | Latest: \u20B9' + data.normal_market.prices.slice(-1)[0].toFixed(2);
                    if (data.close_price) title += ' | Close: \u20B9' + data.close_price.toFixed(2);
                    smoothUpdateChart(
                        normalMarketChartInstance,
                        data.normal_market,
                        lastNormalMarketData,
                        title
                    );
                    lastNormalMarketData = data.normal_market;
                    document.getElementById('normalMarketLastUpdated').textContent = 'Last updated: ' + now;
                }

                var closePrice = data.close_price;
                if (closePrice) {
                    var badge = document.getElementById('closePriceBadge');
                    if (badge) {
                        badge.textContent = 'Closing Price: \u20B9' + closePrice.toFixed(2);
                        badge.classList.remove('d-none');
                    }
                }
            })
            .catch(function (error) { console.error('Error updating live data:', error); });
    }

    function smoothUpdateChart(chartInstance, newData, oldData, newTitle) {
        var newTimes = newData.times;
        var newPrices = newData.prices;

        if (oldData && JSON.stringify(oldData.times) === JSON.stringify(newTimes) &&
            JSON.stringify(oldData.prices) === JSON.stringify(newPrices)) {
            return;
        }

        chartInstance.data.labels = newTimes;
        chartInstance.data.datasets[0].data = newPrices;
        if (newTitle) chartInstance.options.plugins.title.text = newTitle;

        var minPrice = Math.min.apply(null, newPrices.filter(function (p) { return p > 0; }));
        chartInstance.options.scales.y.min = prevClosePrice > 0 ?
            Math.min(minPrice * 0.99, prevClosePrice * 0.99) : minPrice * 0.99;

        chartInstance.update();
    }

    function resetZoom(chartId) {
        var chart = chartId === 'preOpenChart' ? preOpenChartInstance : normalMarketChartInstance;
        if (chart) chart.resetZoom();
    }

    function showAllData(chartId) {
        var chart = chartId === 'preOpenChart' ? preOpenChartInstance : normalMarketChartInstance;
        if (chart) {
            chart.options.scales.x.min = undefined;
            chart.options.scales.x.max = undefined;
            chart.update();
        }
    }

    // ---------------- Bootstrap ----------------
    document.addEventListener('DOMContentLoaded', function () {
        if (!hasSymbol) {
            var lastSymbol = null;
            try { lastSymbol = localStorage.getItem(LS_KEY); } catch (e) { }
            if (lastSymbol && lastSymbol.trim()) {
                window.location.replace('/?symbol=' + encodeURIComponent(lastSymbol.trim().toUpperCase()));
                return;
            }
        }

        initSearch();
        initializeIndicesSlider();

        if (hasSymbol) {
            if (!window.location.hash) {
                var chartsTab = new bootstrap.Tab(document.getElementById('charts-tab'));
                chartsTab.show();
            }

            initializeStockData();
            if (activeTab === 'charts' || !activeTab) {
                initializeCharts();
            } else if (activeTab === 'historical') {
                setTimeout(loadHistorical, 100);
            } else if (activeTab === 'peers') {
                setTimeout(loadPeers, 100);
            }

            var historicalForm = document.getElementById('historicalForm');
            if (historicalForm) {
                historicalForm.addEventListener('submit', function (e) {
                    e.preventDefault();
                    loadHistorical();
                });
            }

            document.querySelectorAll('.nav-tabs .nav-link').forEach(function (tab) {
                tab.addEventListener('click', function () {
                    if (this.id === 'charts-tab' && !preOpenChartInstance && !normalMarketChartInstance) {
                        setTimeout(initializeCharts, 100);
                    }
                    if (this.id === 'historical-tab') {
                        setTimeout(loadHistorical, 100);
                    }
                    if (this.id === 'peers-tab') {
                        setTimeout(loadPeers, 100);
                    }
                });
            });
        }
    });

    // Pause heavy FPS polling when the tab is hidden, refresh on return
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') {
            if (hasSymbol) {
                fetchStockData();
                if (!preOpenChartInstance && !normalMarketChartInstance) initializeCharts();
            }
        }
    });

    // Clean up intervals when leaving the page
    window.addEventListener('beforeunload', function () {
        if (indicesUpdateInterval) clearInterval(indicesUpdateInterval);
        if (stockUpdateInterval) clearInterval(stockUpdateInterval);
    });

    // Expose handlers used by inline onclick attributes in the template
    window.setPeriod = setPeriod;
    window.resetZoom = resetZoom;
    window.showAllData = showAllData;
})();