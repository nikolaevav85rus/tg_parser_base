import csv
from parser import parse_signal

def run_test_and_save():
    success_count = 0
    fail_count = 0
    
    print("=== ЗАПУСК ПАРСЕРА И СОХРАНЕНИЕ РЕЗУЛЬТАТОВ В CSV ===")
    
    try:
        # Открываем исходный файл для чтения и новый файл для записи результатов
        with open('historical_signals.csv', 'r', encoding='utf-8') as f_in, \
             open('parsed_results.csv', 'w', encoding='utf-8', newline='') as f_out:
             
            reader = csv.reader(f_in, delimiter=';')
            writer = csv.writer(f_out, delimiter=';')
            
            # Пропускаем заголовки исходного файла
            headers = next(reader)
            
            # Пишем красивые заголовки для нашего нового файла с результатами
            writer.writerow(['Дата', 'Ожидаемая монета', 'Ожидаемый тип', 'Распознанная монета', 'Распознанный тип', 'Цена входа', 'Тейк-профит', 'Статус', 'Текст сигнала (в одну строку)'])
            
            for row in reader:
                if len(row) < 7: continue
                
                date_str = row[0]
                expected_coin = row[1]
                expected_type = row[2]
                raw_text = row[6]
                
                if not raw_text.strip(): continue
                
                # ПРОГОНЯЕМ ТЕКСТ ЧЕРЕЗ ПАРСЕР
                result = parse_signal(raw_text)
                
                parsed_coin = ""
                parsed_type = ""
                parsed_price = ""
                parsed_target = ""
                status = "ОШИБКА"
                
                if result:
                    parsed_coin = result['coin']
                    parsed_type = result['signal_type']
                    parsed_price = result.get('price') or ""
                    parsed_target = result.get('target_price') or ""
                    
                    if expected_coin in parsed_coin and expected_type == parsed_type:
                        status = "ОК"
                        success_count += 1
                    else:
                        status = "ОШИБКА РАСПОЗНАВАНИЯ"
                        fail_count += 1
                else:
                    if 'СИГНАЛ' in raw_text.upper() or 'TL_indicator_bot' in raw_text:
                        status = "ИГНОР (ОБЗОР/ССЫЛКА)"
                        success_count += 1
                    else:
                        status = "ПРОПУЩЕНО"
                        fail_count += 1
                        
                # Убираем переносы строк из текста сигнала, чтобы CSV таблица не "ломалась" визуально
                flat_text = raw_text.replace('\n', ' ').replace('\r', '')
                        
                # Записываем строку результатов
                writer.writerow([date_str, expected_coin, expected_type, parsed_coin, parsed_type, parsed_price, parsed_target, status, flat_text])
                        
        print(f"\n✅ Готово! Результаты успешно сохранены в файл: parsed_results.csv")
        print(f"✅ Успешно (или верно проигнорировано): {success_count}")
        print(f"❌ Ошибок/Пропусков: {fail_count}")
            
    except FileNotFoundError:
        print("❌ Файл historical_signals.csv не найден в папке.")
    except Exception as e:
        print(f"❌ Ошибка при формировании файла: {e}")

if __name__ == '__main__':
    run_test_and_save()